from functools import partial

import torch
import torch.nn as nn

from timm.models.vision_transformer import PatchEmbed, DropPath, Mlp

from util.pos_embed import get_2d_sincos_pos_embed

from taming.models.vqgan import VQModel
from omegaconf import OmegaConf
import numpy as np
import scipy.stats as stats
from slot_attn import SlotAttentionEncoder
import math



class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

        with torch.cuda.amp.autocast(enabled=False):
            attn = (q.float() @ k.float().transpose(-2, -1)) * self.scale

        attn = attn - torch.max(attn, dim=-1, keepdim=True)[0]
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x, attn


class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

    def forward(self, x, return_attention=False):
        if return_attention:
            _, attn = self.attn(self.norm1(x))
            return attn
        else:
            y, _ = self.attn(self.norm1(x))
            x = x + self.drop_path(y)
            x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class LabelSmoothingCrossEntropy(nn.Module):
    """ NLL loss with label smoothing.
    """
    def __init__(self, smoothing=0.1):
        super(LabelSmoothingCrossEntropy, self).__init__()
        assert smoothing < 1.0
        self.smoothing = smoothing
        self.confidence = 1. - smoothing

    def forward(self, x: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        logprobs = torch.nn.functional.log_softmax(x, dim=-1)
        nll_loss = -logprobs.gather(dim=-1, index=target.unsqueeze(1))
        nll_loss = nll_loss.squeeze(1)
        smooth_loss = -logprobs.mean(dim=-1)
        loss = self.confidence * nll_loss + self.smoothing * smooth_loss
        return loss


class BertEmbeddings(nn.Module):
    """Construct the embeddings from word, position and token_type embeddings."""

    def __init__(self, vocab_size, hidden_size, max_position_embeddings, dropout=0.1):
        super().__init__()
        self.word_embeddings = nn.Embedding(vocab_size, hidden_size)
        self.position_embeddings = nn.Embedding(max_position_embeddings, hidden_size)

        # self.LayerNorm is not snake-cased to stick with TensorFlow model variable name and be able to load
        # any TensorFlow checkpoint file
        self.LayerNorm = nn.LayerNorm(hidden_size, eps=1e-6)
        self.dropout = nn.Dropout(dropout)
        # position_ids (1, len position emb) is contiguous in memory and exported when serialized
        self.register_buffer("position_ids", torch.arange(max_position_embeddings).expand((1, -1)))

        torch.nn.init.normal_(self.word_embeddings.weight, std=.02)
        torch.nn.init.normal_(self.position_embeddings.weight, std=.02)

    def forward(
        self, input_ids
    ):
        input_shape = input_ids.size()

        seq_length = input_shape[1]

        position_ids = self.position_ids[:, :seq_length]

        inputs_embeds = self.word_embeddings(input_ids)

        position_embeddings = self.position_embeddings(position_ids)
        embeddings = inputs_embeds + position_embeddings

        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings


class MlmLayer(nn.Module):

    def __init__(self, feat_emb_dim, word_emb_dim, vocab_size):
        super().__init__()
        self.fc = nn.Linear(feat_emb_dim, word_emb_dim)
        self.gelu = nn.GELU()
        self.ln = nn.LayerNorm(word_emb_dim)
        self.bias = nn.Parameter(torch.zeros(1, 1, vocab_size))

    def forward(self, x, word_embeddings):
        mlm_hidden = self.fc(x)
        mlm_hidden = self.gelu(mlm_hidden)
        mlm_hidden = self.ln(mlm_hidden)
        word_embeddings = word_embeddings.transpose(0, 1)
        logits = torch.matmul(mlm_hidden, word_embeddings)
        logits = logits + self.bias
        return logits


class MaskedGenerativeEncoderViT(nn.Module):
    """ Masked Autoencoder with VisionTransformer backbone
    """
    def __init__(self, img_size=256, patch_size=16, in_chans=3,
                 embed_dim=1024, depth=24, num_heads=16,
                 decoder_embed_dim=512, decoder_depth=8, decoder_num_heads=16,
                 mlp_ratio=4., norm_layer=nn.LayerNorm, norm_pix_loss=False,
                 mask_ratio_min=0.5, mask_ratio_max=1.0, mask_ratio_mu=0.55, mask_ratio_std=0.25,epsilon=1e-8,
                 vqgan_ckpt_path='vqgan_jax_strongaug.ckpt'):
        super().__init__()

        self.epsilon = epsilon
        # --------------------------------------------------------------------------
        # VQGAN specifics
        config = OmegaConf.load('config/vqgan.yaml').model
        self.vqgan = VQModel(ddconfig=config.params.ddconfig,
                             n_embed=config.params.n_embed,
                             embed_dim=config.params.embed_dim,
                             ckpt_path=vqgan_ckpt_path)
        for param in self.vqgan.parameters():
            param.requires_grad = False

        self.codebook_size = config.params.n_embed
        vocab_size = self.codebook_size + 1000 + 1  # 1024 codebook size, 1000 classes, 1 for mask token.
        self.fake_class_label = self.codebook_size + 1100 - 1024
        self.mask_token_label = vocab_size - 1
        self.token_emb = BertEmbeddings(vocab_size=vocab_size,
                                        hidden_size=embed_dim,
                                        max_position_embeddings=256+1,
                                        dropout=0.1)

        # MAGE variant masking ratio
        self.mask_ratio_min = mask_ratio_min
        self.mask_ratio_generator = stats.truncnorm((mask_ratio_min - mask_ratio_mu) / mask_ratio_std,
                                                    (mask_ratio_max - mask_ratio_mu) / mask_ratio_std,
                                                    loc=mask_ratio_mu, scale=mask_ratio_std)

        # --------------------------------------------------------------------------
        # MAGE encoder specifics
        dropout_rate = 0.1
        self.patch_embed = PatchEmbed(img_size, patch_size, in_chans, embed_dim)
        num_patches = self.patch_embed.num_patches

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim), requires_grad=False)  # fixed sin-cos embedding

        self.blocks = nn.ModuleList([
            Block(embed_dim, num_heads, mlp_ratio, qkv_bias=True, qk_scale=None, norm_layer=norm_layer,
                  drop=dropout_rate, attn_drop=dropout_rate)
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)
        # --------------------------------------------------------------------------
        self.slot_attention = SlotAttentionEncoder(
            num_iterations=3,  # specify the number of iterations
            num_slots=7,       # specify the number of slots
            input_channels=embed_dim,  # since it should match the output of your encoder
            slot_size=768,       # specify the slot size
            mlp_hidden_size=1024, # specify the MLP hidden size
            pos_channels=4,    # specify the positional channels size
            truncate='none', # or other options as per your requirement
            init_method='shared_gaussian',  # or 'shared_gaussian'
            num_heads=6,       # specify the number of heads for attention
            drop_path=0.0        # specify dropout path rate
        )

        # --------------------------------------------------------------------------
        # MAGE decoder specifics
        self.decoder_embed = nn.Linear(embed_dim, decoder_embed_dim, bias=True)

        self.mask_token = nn.Parameter(torch.zeros(1, 1, decoder_embed_dim))
        self.pad_with_cls_token = True

        self.decoder_pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, decoder_embed_dim), requires_grad=False)  # fixed sin-cos embedding
        self.decoder_pos_embed_learned = nn.Parameter(torch.zeros(1, num_patches + 1, decoder_embed_dim))  # learnable pos embedding

        self.decoder_blocks = nn.ModuleList([
            Block(decoder_embed_dim, decoder_num_heads, mlp_ratio, qkv_bias=True, qk_scale=None, norm_layer=norm_layer,
                  drop=dropout_rate, attn_drop=dropout_rate)
            for i in range(decoder_depth)])

        self.decoder_norm = norm_layer(decoder_embed_dim)
        self.decoder_pred = nn.Linear(decoder_embed_dim, patch_size**2 * in_chans, bias=True) # decoder to patch
        # --------------------------------------------------------------------------

        # --------------------------------------------------------------------------
        # MlmLayer
        self.mlm_layer = MlmLayer(feat_emb_dim=decoder_embed_dim, word_emb_dim=embed_dim, vocab_size=vocab_size)

        self.norm_pix_loss = norm_pix_loss

        self.criterion = LabelSmoothingCrossEntropy(smoothing=0.1)

        self.initialize_weights()

    def initialize_weights(self):
        # initialization
        # initialize (and freeze) pos_embed by sin-cos embedding
        pos_embed = get_2d_sincos_pos_embed(self.pos_embed.shape[-1], int(self.patch_embed.num_patches**.5), cls_token=True)
        self.pos_embed.data.copy_(torch.from_numpy(pos_embed).float().unsqueeze(0))

        decoder_pos_embed = get_2d_sincos_pos_embed(self.decoder_pos_embed.shape[-1], int(self.patch_embed.num_patches**.5), cls_token=True)
        self.decoder_pos_embed.data.copy_(torch.from_numpy(decoder_pos_embed).float().unsqueeze(0))

        # initialize patch_embed like nn.Linear (instead of nn.Conv2d)
        w = self.patch_embed.proj.weight.data
        torch.nn.init.xavier_uniform_(w.view([w.shape[0], -1]))

        # timm's trunc_normal_(std=.02) is effectively normal_(std=0.02) as cutoff is too big (2.)
        torch.nn.init.normal_(self.cls_token, std=.02)
        torch.nn.init.normal_(self.mask_token, std=.02)
        torch.nn.init.normal_(self.decoder_pos_embed_learned, std=.02)

        # initialize nn.Linear and nn.LayerNorm
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            # we use xavier_uniform following official JAX ViT:
            torch.nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward_encoder(self, x):
        # tokenization
        with torch.no_grad():
            z_q, _, token_tuple = self.vqgan.encode(x)

        #z_q torch.Size([32, 256, 16, 16])
        _, _, token_indices = token_tuple
        token_indices = token_indices.reshape(z_q.size(0), -1)
        gt_indices = token_indices.clone().detach().long()
        #token_indices 256,256,256.... batch size times
        #gt_indices torch.Size([32, 256])

        # masking
        bsz, seq_len = token_indices.size()

        token_drop_mask = torch.zeros(bsz, seq_len, device=x.device).float()  # No tokens are dropped
        token_all_mask = torch.zeros(bsz, seq_len, device=x.device).float()    # Mask no tokens
        #both torch.Size([32, 256])
        token_indices[token_all_mask.nonzero(as_tuple=True)] = self.mask_token_label
        # print("Masekd num token:", torch.sum(token_indices == self.mask_token_label, dim=1))

        # concate class token
        token_indices = torch.cat([torch.zeros(token_indices.size(0), 1).cuda(device=token_indices.device), token_indices], dim=1)
        token_indices[:, 0] = self.fake_class_label
        token_drop_mask = torch.cat([torch.zeros(token_indices.size(0), 1).cuda(), token_drop_mask], dim=1)
        token_all_mask = torch.cat([torch.zeros(token_indices.size(0), 1).cuda(), token_all_mask], dim=1)
        token_indices = token_indices.long()
        #torch.Size([32, 257])
        # bert embedding
        input_embeddings = self.token_emb(token_indices)
        # print("Input embedding shape:", input_embeddings.shape)
        bsz, seq_len, emb_dim = input_embeddings.shape

        # dropping
        token_keep_mask = 1 - token_drop_mask
        #input_embeddings_after_drop = input_embeddings[token_keep_mask.nonzero(as_tuple=True)].reshape(bsz, -1, emb_dim)
        # print("Input embedding after drop shape:", input_embeddings_after_drop.shape)

        # apply Transformer blocks
        #x = input_embeddings_after_drop
        x = input_embeddings

        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)
        # print("Encoder representation shape:", x.shape)
        # Mask all tokens for the decoding phase
        _, _, token_indices = token_tuple
        token_indices = token_indices.reshape(z_q.size(0), -1)
        gt_indices = token_indices.clone().detach().long()

        # masking
        bsz, seq_len = token_indices.size()
        mask_ratio_min = self.mask_ratio_min
        mask_rate = self.mask_ratio_generator.rvs(1)[0]

        num_dropped_tokens = int(np.ceil(seq_len * mask_ratio_min))
        num_masked_tokens = int(np.ceil(seq_len * mask_rate))

        # it is possible that two elements of the noise is the same, so do a while loop to avoid it
        while True:
            noise = torch.rand(bsz, seq_len, device=x.device)  # noise in [0, 1]
            sorted_noise, _ = torch.sort(noise, dim=1)  # ascend: small is remove, large is keep
            cutoff_mask = sorted_noise[:, num_masked_tokens-1:num_masked_tokens]
            token_all_mask = (noise <= cutoff_mask).float()
            if token_all_mask.sum() == bsz*num_masked_tokens:
                break
            else:
                print("Rerandom the noise!")
        # print(mask_rate, num_dropped_tokens, num_masked_tokens, token_drop_mask.sum(dim=1), token_all_mask.sum(dim=1))
        token_indices[token_all_mask.nonzero(as_tuple=True)] = self.mask_token_label
        # print("Masekd num token:", torch.sum(token_indices == self.mask_token_label, dim=1))

        # concate class token
        token_indices = torch.cat([torch.zeros(token_indices.size(0), 1).cuda(device=token_indices.device), token_indices], dim=1)
        token_indices[:, 0] = self.fake_class_label
        # token_drop_mask = torch.cat([torch.zeros(token_indices.size(0), 1).cuda(), token_drop_mask], dim=1)
        token_all_mask = torch.cat([torch.zeros(token_indices.size(0), 1).cuda(), token_all_mask], dim=1)
        token_indices = token_indices.long()

        return x, gt_indices, token_drop_mask, token_all_mask
    

    def forward_decoder_generation(self, x, token_drop_mask, token_all_mask):
        # embed tokens
        x = self.decoder_embed(x)

        # append mask tokens to sequence
        if self.pad_with_cls_token:
            mask_tokens = x[:, 0:1].repeat(1, token_all_mask.shape[1], 1)
        else:
            mask_tokens = self.mask_token.repeat(token_all_mask.shape[0], token_all_mask.shape[1], 1)

        # put undropped tokens into original sequence
        x_after_pad = mask_tokens.clone()
        x_after_pad[(1 - token_drop_mask).nonzero(as_tuple=True)] = x.reshape(x.shape[0] * x.shape[1], x.shape[2])
        # set undropped but masked positions with mask
        x_after_pad = torch.where(token_all_mask.unsqueeze(-1).bool(), mask_tokens, x_after_pad)

        # add pos embed
        x = x_after_pad + self.decoder_pos_embed_learned

        # apply Transformer blocks
        for blk in self.decoder_blocks:
            x = blk(x)

        x = self.decoder_norm(x)

        word_embeddings = self.token_emb.word_embeddings.weight.data.detach()
        x = self.mlm_layer(x, word_embeddings)
        # print("Logits shape:", x.shape)

        return x

    def forward_decoder(self, x,slots, token_drop_mask, token_all_mask):
        # embed tokens
        x = self.decoder_embed(x)

        # append mask tokens to sequence
        if self.pad_with_cls_token:
            mask_tokens = x[:, 0:1].repeat(1, token_all_mask.shape[1], 1)
        else:
            mask_tokens = self.mask_token.repeat(token_all_mask.shape[0], token_all_mask.shape[1], 1)

        # put undropped tokens into original sequence
        x_after_pad = mask_tokens.clone()
        x_after_pad[(1 - token_drop_mask).nonzero(as_tuple=True)] = x.reshape(x.shape[0] * x.shape[1], x.shape[2])
        # set undropped but masked positions with mask
        x_after_pad = torch.where(token_all_mask.unsqueeze(-1).bool(), mask_tokens, x_after_pad)

        # add pos embed
        x = x_after_pad + self.decoder_pos_embed_learned

        x = torch.cat((slots, x), dim=1)

        # apply Transformer blocks
        # for blk in self.decoder_blocks:
        #     x = blk(x)
        for i, blk in enumerate(self.decoder_blocks):
            if i == len(self.decoder_blocks) - 1: # last block
                # Get attention matrix from last block
                with torch.no_grad(): # r
                    atts = blk(x, return_attention=True)
            x = blk(x)

        x = self.decoder_norm(x)

        word_embeddings = self.token_emb.word_embeddings.weight.data.detach()
        x = self.mlm_layer(x, word_embeddings)
        # print("Logits shape:", x.shape)

        #print(atts.shape)
        #[32,16,264,264]
        atts=atts.sum(dim=1)
        atts_slots = atts[:,8:,:7]
        atts_slots=atts_slots+self.epsilon
        sums = atts_slots.sum(dim=2, keepdim=True)
        # Replace zero sums to avoid division by zero
        normalized_atts_slots = atts_slots / sums
        #[32,256,7]

        
        return x,normalized_atts_slots


    # [19:16:56.286655] 32
    # [19:16:56.286734] 256
    # [19:16:56.286754] torch.Size([32, 257])
    # [19:16:56.286786] torch.Size([32, 264, 2025])
    # [19:16:56.492483] torch.Size([8192])
    # [19:16:56.492559] torch.Size([32, 256])
    def forward_loss(self, gt_indices, logits, mask):
        bsz, seq_len = gt_indices.size()
        # logits and mask are with seq_len+1 but gt_indices is with seq_len
        loss = self.criterion(logits[:, 8:, :self.codebook_size].reshape(bsz*seq_len, -1), gt_indices.reshape(bsz*seq_len))#DEN EIMAI SIGOUROS GIA TO +1 H +7
        # loss = self.criterion(logits[:, 1:, :self.codebook_size].reshape(bsz*seq_len, -1), gt_indices.reshape(bsz*seq_len))#DEN EIMAI SIGOUROS GIA TO +1 H +7

        # print(loss.shape)
        loss = loss.reshape(bsz, seq_len)
        # print(loss.shape)
        loss = (loss * mask[:, 1:]).sum() / mask[:, 1:].sum()  # mean loss on removed patches
        # print(loss)
        # print("Telos")
        return loss

    def forward(self, imgs):

        latent, gt_indices, token_drop_mask, token_all_mask = self.forward_encoder(imgs)
        #slots, attn, init_slots, attn_logits = self.slot_attention(latent[:,1:,:])
        slots, attn, init_slots, attn_logits = self.slot_attention(latent)

        # print(latent.shape)
        # logits = self.forward_decoder(latent, token_drop_mask, token_all_mask)
        # logits,attn_dec = self.forward_decoder(latent,latent ,token_drop_mask, token_all_mask)

        logits,attn_dec = self.forward_decoder(latent,slots ,token_drop_mask, token_all_mask)
        #[Batch,decoder264,2025]


        loss = self.forward_loss(gt_indices, logits, token_all_mask)

        return loss, imgs, token_all_mask,attn[:,1:,:],attn_dec,logits

    def freeze_encoder_decoder(self):
        # Freeze encoder
        self.cls_token.requires_grad = False
        for param in self.patch_embed.parameters():
            param.requires_grad = False
        for block in self.blocks:
            for param in block.parameters():
                param.requires_grad = False

        # Freeze decoder
        self.mask_token.requires_grad = False
        self.decoder_pos_embed_learned.requires_grad = False
        for param in self.decoder_norm.parameters():
            param.requires_grad = False
        for block in self.decoder_blocks:
            for param in block.parameters():
                param.requires_grad = False
        for param in self.decoder_embed.parameters():
            param.requires_grad = False
        for param in self.decoder_pred.parameters():
            param.requires_grad = False
        # Add any other components as needed


def mage_vit_base_patch16(**kwargs):
    model = MaskedGenerativeEncoderViT(
        patch_size=16, embed_dim=768, depth=12, num_heads=12,
        decoder_embed_dim=768, decoder_depth=8, decoder_num_heads=16,
        mlp_ratio=4, norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)

    model.freeze_encoder_decoder()

    return model


def mage_vit_large_patch16(**kwargs):
    model = MaskedGenerativeEncoderViT(
        patch_size=16, embed_dim=1024, depth=24, num_heads=16,
        decoder_embed_dim=1024, decoder_depth=8, decoder_num_heads=16,
        mlp_ratio=4, norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    
    model.freeze_encoder_decoder()

    return model
