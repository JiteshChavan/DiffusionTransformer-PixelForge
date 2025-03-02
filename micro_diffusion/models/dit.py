from typing import List
from .modules import CaptionProjection, CrossAttention, MLP, MLPConfig, SelfAttention, T2IFinalLayer, TimeStepEmbedder
from .modules import create_norm, get_2d_sincos_pos_embed, get_mask, mask_out_token, modulate, insert_filler_masked_tokens


import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
from timm.models.vision_transformer import PatchEmbed

class AttentionBlockPromptEmbedding(nn.Module):
    """Attention block specifically for processing prompt embeddings.
    
    Args:
        n_embd(int) : Input and output dimension
        head_size (int): Channels per attention head
        # TODO: change name, fan_hidden multiplier
        mlp_n_hidden_mult (float): Multiplier for feed-forward network hidden dimension w.r.t input dimension
        # TODO: change name, something to do with layerwise scaling of n_embd
        hidden_base_mult(int): Round feed-forward network fan_hidden up to nearest multiple of this value
        norm_eps (float): Epsilon value for layer normalization (avoid division by 0)
        use_bias (bool): whether to use bias in linear layers
    """

    def __init__ (self, n_embd, head_size, mlp_n_hidden_mult, hidden_base_mult, norm_eps, use_bias):
        super().__init__()
        assert n_embd % head_size == 0, f"n_embd:{n_embd} should be divisble by head_size:{head_size}"

        self.n_embd = n_embd
        self.n_head = n_embd // head_size

        self.ln1 = create_norm ("layernorm", n_embd, eps=norm_eps)
        # Not specifying n_hidden implies qkv will project n_embd into 3xn_embd
        self.attn = SelfAttention (n_embd, self.n_head, qkv_bias=use_bias, norm_eps=norm_eps)

        self.ln2 = create_norm ("layernorm", n_embd, eps=norm_eps)
        self.ffn = FeedForwardNetwork (n_embd, n_hidden=int(n_embd*mlp_n_hidden_mult), hidden_base_mult=hidden_base_mult, use_bias=use_bias)
    
    def forward (self, x, **kwargs):
        x = x + self.attn (self.ln1(x))
        x = x + self.ffn (self.ln2(x))
        return x
    
    def custom_init (self, init_std:float =0.02):
        self.attn.custom_init(init_std=init_std)
        self.ffn.custom_init(init_std=init_std)

class FeedForwardNetwork(nn.Module):
    """Feed-forward block with SiLU activation
        n_embd: input/output channel dimensionality
        n_hidden : hidden layer dimensionality
        hidden_base_mult : make n_hidden nearest next multiple of this hidden_base_mult
        use_bias: Use bias in linear layers or not
    """
    def __init__(self, n_embd, n_hidden, hidden_base_mult, use_bias):
        super().__init__()
        
        self.n_hidden = int (2*n_hidden/3)
        self.n_hidden = hidden_base_mult * ((n_hidden + hidden_base_mult - 1) // hidden_base_mult)
        self.fc1 = nn.Linear (n_embd, self.n_hidden, bias=use_bias)
        self.fc2 = nn.Linear (n_embd, self.n_hidden, bias=use_bias)
        self.fc3 = nn.Linear(self.n_hidden, n_embd, bias=use_bias)

    def forward (self, x):
        return self.fc3 (F.silu(self.fc1(x)) * self.fc2(x))
    
    def custom_init (self, init_std:float):
        nn.init.trunc_normal_(self.fc1.weight, mean=0.0, std=0.02)
        for linear in (self.fc2, self.fc3):
            nn.init.trunc_normal_(linear.weight, mean=0.0, std=init_std)
    
class FeedForwardECMoe (nn.Module):
    """Expert Choice style Mixture of Experts feed forward layer with GELU activation
    
    Args:
        num_experts (int) : number of experts in the layer
        expert_capacity (float): capacity factor determining tokens per expert
        n_embd (int): Input and output dimension
        n_hidden (int): hidden layer channels
        hidden_base_mult (int) : Round hidden dimension upto next nearest multiple of this value
    """

    def __init__(self, num_experts, expert_capacity:float, n_embd, n_hidden, hidden_base_mult):
        self.num_experts = num_experts

        # scaling that restricts or boosts capacity of an expert (eg: 0.5x or 1.5x) we default to 1.0 I think
        self.expert_capacity = expert_capacity
        
        self.n_embd = n_embd
        self.hidden_base_mult = hidden_base_mult
        self.n_hidden = hidden_base_mult * ((n_hidden + hidden_base_mult -1) // hidden_base_mult)

        # to get softmax over num_experts for T tokens
        self.gate = nn.Linear (n_embd, num_experts, bias=False) # bias false makes sense in case model wants to 1 hot on experts

        # each expert goes from n_embd to n_hidden
        self.w1 = nn.Parameter (torch.ones (num_experts, n_embd, n_hidden))
        # non linear activation
        self.gelu = nn.GELU()
        # each expert goes from n_hidden to n_embd
        self.w2 = nn.Parameter (torch.ones (num_experts, n_hidden, n_embd))
    
    def forward (self, x:torch.Tensor):
        # extract shapes
        assert x.dim() == 3
        B, T, C = x.shape

        
        tokens_per_expert = int( self.expert_capacity * T / self.num_experts)

        # get scores, softmax for each token over experts, how appealing is an expert to each of the T tokens
        scores = self.gate (x) # (B, T, E) E is number of experts
        probs = F.softmax (scores, dim=-1) # probs for T tokens across experts

        probs_expert_looking_at_all_tokens = probs.permute(0, 2, 1) # (B, E, T)

        # gather top-tokens-per-expert
        # probs, idices
        expert_specific_token_probs, expert_specific_tokens = torch.topk (probs_expert_looking_at_all_tokens, tokens_per_expert, dim=-1)
        # (B, E, l)       (B, E, l) l is tokens per expert
        # create one hot vectors of T size for the selected tokens, so that we can extract from B,T,C
        # to construct xin for moe
        extract_from_x_one_hot = F.one_hot(expert_specific_tokens, num_classes=T).float() # (B, E, l, T)

        # Goal: (B, E, l C) from x
        xin = torch.einsum ('BElT, BTC -> BElC', extract_from_x_one_hot, x)
        
        # forward
        activation = torch.einsum ('BElC, ECH -> BElH', xin, self.w1) # (B, E, l, H)
        activation = self.gelu(activation)
        activation = torch.einsum ('BElH, EHC -> BElC', activation, self.w2) # (B, E, l, C)

        # scale the activation with gating score probs, so that stronger experts have greater influence on the outputs
        activation = activation * expert_specific_token_probs.unsqueeze(dim=-1)

        # use inner product to combine results of T tokens from all the different experts
        out = torch.einsum ('BElC, BElT -> BTC', activation, extract_from_x_one_hot)
        return out
    
    def custom_init (self, init_std:float):
        nn.init.trunc_normal_(self.gate.weight, mean=0.0, std=0.02)
        nn.init.trunc_normal_(self.w1, mean=0.0, std=0.02)
        nn.init.trunc_normal_(self.w2, mean=0.0, std=init_std)

class DiTBlock (nn.Module):
    """DiT transformer block comprising Attention and MLP blocks. It supports choosing between dense feed-forward
    and expert choice style Mixture of Experts feed forward blocks.

    Args:
        n_embd (int): Input and Output dimension of the block
        head_size (int) : Channels per attention head
        mlp_n_hidden_mult (float):  Multiplier for feed-forward network hidden dimension w.r.t input dimension
        qkv_n_hidden_mult (float): Multiplier for dimension in qkv layers in attention block
        hidden_base_mult (int): Round hidden dimension upto next nearest multiple of this value
        pooled_caption_n_embd (int): Dimension of pooled caption embeddings
        norm_eps (float): Epsilon for layer normalization
        
        Setting this flag true, respects depth of blocks and initalizes projection layer weights with respect to block
        indices, to counteract effect of residual additions
        better than GPT2, monolithic init through out network which doesnt respect depth of blocks

        depth_init (bool): Whether to initialize weights of the last layer in MLP/Attention block based on block index
        
        block_index (int): Index of this block in dit model, starts with 0
        num_blocks (int): total number of blocks in the dit model
        scale_cx_attn_n_hidden (bool): whether to scale cross attention qkv dimension using qkv_n_hidden_mult
        use_bias (bool) : whether to use bias in linear layers
        use_moe (bool) : whether to use mixture of experts for MLP block
        num_experts (int) : Number of experts if using MoE block
        expert_capacity (float) : Capacity factor for each expert if using MoE block
    """

    def __init__ (
            self,
            n_embd:int,
            head_size:int,
            mlp_n_hidden_mult:float,
            qkv_n_hidden_mult:float,
            hidden_base_mult:int,
            pooled_caption_n_embd:int,
            norm_eps:float,
            depth_init:bool,
            block_index:int,
            num_blocks:int,
            scale_cx_attn_n_hidden:bool,
            use_bias:bool,
            use_moe:bool,
            num_experts:int,
            expert_capacity:float
    ):
        super().__init__()
        self.n_embd = n_embd

        assert n_embd % head_size == 0, f"n_embd:{n_embd} is not divisible by head_size:{head_size}"

        if qkv_n_hidden_mult == 1:
            qkv_n_hidden = n_embd
        else:
            # round qkv_n_hidden up to be next nearest multiple of 2*head_size
            # qkv_n_hidden % headsize = 0
            qkv_n_hidden = 2*head_size* (( int (qkv_n_hidden_mult *n_embd) + (2*head_size) - 1) // (2*head_size))
        

        self.ln1 = create_norm ("layernorm", n_embd, eps=norm_eps)
        # self attention on token sequence input to DiT
        self.attn = SelfAttention (n_embd, qkv_n_hidden // head_size, qkv_bias=use_bias, n_hidden=qkv_n_hidden, norm_eps=norm_eps)
        self.ln2 = create_norm ("layernorm", dim=n_embd, eps=norm_eps)

        # cross attention TODO: check if its done against time(noise) or caption embeddings
        # TODO: cross attention (IF run on CAPTIONS has to have 0 contribution (use_bias=False) when caption set to 0 for CFG)
        cx_attn_n_hidden = qkv_n_hidden if scale_cx_attn_n_hidden else n_embd
        self.cx_attn = CrossAttention (n_embd=n_embd, n_head=cx_attn_n_hidden//head_size, n_hidden=cx_attn_n_hidden, norm_eps=norm_eps, qkv_bias=use_bias)

        self.ln3 = create_norm ("layernorm", dim=n_embd, eps=norm_eps)

        mlp_n_hidden = int (n_embd * mlp_n_hidden_mult)
        self.mlp = (
            FeedForwardECMoe (num_experts=num_experts, expert_capacity=expert_capacity, 
                              n_embd=n_embd, n_hidden=mlp_n_hidden, 
                              hidden_base_mult= hidden_base_mult) if use_moe else
            FeedForwardNetwork (n_embd=n_embd, n_hidden=mlp_n_hidden, hidden_base_mult=hidden_base_mult, use_bias=use_bias)
        )

        self.AdaLN_modulation = nn.Sequential (
            nn.GELU(approximate="tanh"),
            nn.Linear (pooled_caption_n_embd, 6*n_embd)
        )

        # Equivalent to NANO_GPT_SCALE_INIT for projecting layers
        # std dev from which projection layers weights are to be initated
        # we hard code rest of the layers as 0.02
        self.weight_init_std = (
            0.02 / (3 * (block_index + 1)) ** 0.5 if depth_init else
            0.02 / (3 * num_blocks) ** 0.5
        )

    #change name of args after setting up initial aggregation
    # x is forward information stream
    # c is aggregated caption and time to certain abstraction
    # TODO: introspect later t is lets just say time (B, C) for now

    def forward(self, x:torch.Tensor, c:torch.Tensor, t:torch.Tensor):
        # extract 3 gamma, 3 beta from pooled caption embd?
        # each has shape (B, C)
        shift_msa, scale_msa, gate_msa, shift_mlp, scale_mlp, gate_mlp =self.AdaLN_modulation (t).split (self.n_embd, dim=-1) # split along 6c so we get 6 splits

        # x (B, T, C) after modulation with scale (B, [1],C) and shift (B, [1], C) //[1] unsqueezed
        # the shape of x is still (B, T, C) it needs to be gated with (B, [1], C) so that the shape will still be (B, T, C)
        # B, T, C = B, 1, C * B, T, C
        x = x + gate_msa.unqueeze(1) * self.attn(modulate( self.ln1(x), shift=shift_msa, scale=scale_msa))
        x = x + self.cx_attn (self.ln2(x), c) # caption condition c already has extracted information from timestep at presetup
        x = x + gate_mlp.unsqueeze(1)*self.mlp(modulate(self.ln3(x), shift=shift_mlp, scale=scale_mlp)) # (B, T, C)

        return x # (B, T, C)
    
    def custom_init(self):
        # reset affine parameters to 1 and 0 respectively
        for norm in (self.ln1, self.ln2, self.ln3):
            norm.reset_parameters()
        
        # initialize layers with HARDCODED calculate correct fan to keep exploding variance in check
        # initalize projecting layers while taking into consideration the effect of residual additions
        self.attn.custom_init(self.weight_init_std)
        self.cx_attn.custom_init(self.weight_init_std)
        self.mlp.custom_init(self.weight_init_std)
    







        