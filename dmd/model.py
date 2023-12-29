import torch
import torch.nn.functional as F
from diffusers.utils.torch_utils import randn_tensor


def prepare_latents(unet, vae, batch_size, device, dtype, generator=None):
    num_channels_latents = unet.config.in_channels
    vae_scale_factor = 2 ** (len(vae.config.block_out_channels) - 1)
    height = unet.config.sample_size * vae_scale_factor
    width = unet.config.sample_size * vae_scale_factor
    shape = (batch_size, num_channels_latents, height // vae_scale_factor, width // vae_scale_factor)
    latents = randn_tensor(shape, generator=generator, device=device, dtype=dtype)
    return latents


def encode_prompt(captions, text_encoder, tokenizer):
    with torch.no_grad():
        text_inputs = tokenizer(
            captions,
            padding="max_length",
            max_length=tokenizer.model_max_length,
            truncation=True,
            return_tensors="pt"
        )
        text_input_ids = text_inputs.input_ids
        prompt_embeds = text_encoder(text_input_ids.to(text_encoder.device))[0]
    return prompt_embeds


def generate(unet, scheduler, latents, prompt_embeds):
    bsz = latents.shape[0]
    timesteps = torch.full((bsz,), scheduler.config.num_train_timesteps-1, device=latents.device)
    timesteps = timesteps.long()

    noise_pred = unet(
        latents,
        timesteps,
        encoder_hidden_states=prompt_embeds,
    ).sample

    latents = eps_to_mu(scheduler, noise_pred, latents, timesteps)
    return latents


def eps_to_mu(scheduler, model_output, sample, timesteps):
    alphas_cumprod = scheduler.alphas_cumprod.to(device=sample.device, dtype=sample.dtype)
    alpha_prod_t = alphas_cumprod[timesteps]
    while len(alpha_prod_t.shape) < len(sample.shape):
        alpha_prod_t = alpha_prod_t.unsqueeze(-1)
    beta_prod_t = 1 - alpha_prod_t
    pred_original_sample = (sample - beta_prod_t ** (0.5) * model_output) / alpha_prod_t ** (0.5)
    return pred_original_sample


def distribution_matching_loss(real_unet, fake_unet, noise_scheduler,
                               latents, prompt_embeds, negative_prompt_embeds, args):
    bsz = latents.shape[0]
    min_dm_step = int(noise_scheduler.config.num_train_timesteps * args.min_dm_step_ratio)
    max_dm_step = int(noise_scheduler.config.num_train_timesteps * args.max_dm_step_ratio)

    timestep = torch.randint(min_dm_step, max_dm_step, (bsz,), device=latents.device).long()
    noise = torch.randn_like(latents)
    noisy_latents = noise_scheduler.add_noise(latents, noise, timestep)

    with torch.no_grad():
        noise_pred = fake_unet(
            noisy_latents, timestep, encoder_hidden_states=prompt_embeds.float()
        ).sample
        pred_fake_latents = eps_to_mu(noise_scheduler, noise_pred, noisy_latents, timestep)

        noisy_latents_input = torch.cat([noisy_latents] * 2)
        timestep_input = torch.cat([timestep] * 2)
        prompt_embeds = torch.cat([negative_prompt_embeds, prompt_embeds], dim=0)
        noise_pred = real_unet(
            noisy_latents_input, timestep_input, encoder_hidden_states=prompt_embeds.float()
        ).sample
        noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
        noise_pred = noise_pred_uncond + args.guidance_scale * (noise_pred_text - noise_pred_uncond)

        pred_real_latents = eps_to_mu(noise_scheduler, noise_pred, noisy_latents, timestep)

    weighting_factor = torch.abs(latents - pred_real_latents).mean(dim=[1, 2, 3], keepdim=True)

    grad = (pred_fake_latents - pred_real_latents) / weighting_factor
    loss = F.mse_loss(latents, stopgrad(latents - grad))
    return loss


def stopgrad(x):
    return x.detach()