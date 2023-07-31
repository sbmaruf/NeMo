from nemo.core.classes.common import Serialization
from nemo.core.config import hydra_runner
from typing import List, Optional, Tuple, Union
from omegaconf import OmegaConf
import torch
import pathlib
import numpy
from PIL import Image
from tqdm import tqdm
from enum import Enum
from nemo.collections.multimodal.models.stable_diffusion.diffusion_engine import DiffusionEngine
from nemo.collections.multimodal.parts.stable_diffusion.sdxl_helpers import (
    do_img2img,
    do_sample,
    Img2ImgDiscretizationWrapper
)
from nemo.collections.multimodal.modules.stable_diffusion.diffusionmodules.sampling import (
    EulerEDMSampler,
    HeunEDMSampler,
    EulerAncestralSampler,
    DPMPP2SAncestralSampler,
    DPMPP2MSampler,
    LinearMultistepSampler,
)
from nemo.collections.multimodal.parts.stable_diffusion.sdxl_helpers import perform_save_locally, get_input_image_tensor



class SamplingPipeline:
    def __init__(
        self,
        config_path,
        device="cuda",
        use_fp16=True,
    ) -> None:
        self.config = config_path
        self.device = device
        self.config = OmegaConf.load(self.config)
        self.model = DiffusionEngine(self.config.model).to(self.device)
        if use_fp16:
            model.conditioner.half()
            model.model.half()
        self.vae_scale_factor = 2 ** (len(self.config.model.first_stage_config.ddconfig.ch_mult) - 1)


    def text_to_image(
        self,
        params,
        prompt: str,
        negative_prompt: str = "",
        samples: int = 1,
        return_latents: bool = False,
    ):
        sampler = get_sampler_config(params)
        value_dict = OmegaConf.to_container(params, resolve=True)
        value_dict["prompt"] = prompt
        value_dict["negative_prompt"] = negative_prompt
        value_dict["target_width"] = params.width
        value_dict["target_height"] = params.height
        return do_sample(
            self.model,
            sampler,
            value_dict,
            samples,
            params.height,
            params.width,
            self.config.model.unet_config.in_channels,
            self.vae_scale_factor,
            force_uc_zero_embeddings=["txt"] if not self.config.model.is_legacy else [],
            return_latents=return_latents,
            filter=None,
        )

    def image_to_image(
        self,
        params,
        image,
        prompt: str,
        negative_prompt: str = "",
        samples: int = 1,
        return_latents: bool = False,
    ):
        sampler = get_sampler_config(params)

        if params.img2img_strength < 1.0:
            sampler.discretization = Img2ImgDiscretizationWrapper(
                sampler.discretization,
                strength=params.img2img_strength,
            )
        height, width = image.shape[2], image.shape[3]
        value_dict = OmegaConf.to_container(params, resolve=True)
        value_dict["prompt"] = prompt
        value_dict["negative_prompt"] = negative_prompt
        value_dict["target_width"] = width
        value_dict["target_height"] = height
        return do_img2img(
            image,
            self.model,
            sampler,
            value_dict,
            samples,
            force_uc_zero_embeddings=["txt"] if not self.config.model.is_legacy else [],
            return_latents=return_latents,
            filter=None,
        )

    def refiner(
        self,
        params,
        image,
        prompt: str,
        negative_prompt: Optional[str] = None,
        samples: int = 1,
        return_latents: bool = False,
    ):
        sampler = get_sampler_config(params)
        if params.img2img_strength < 1.0:
            sampler.discretization = Img2ImgDiscretizationWrapper(
                sampler.discretization,
                strength=params.img2img_strength,
            )
        value_dict = {
            "orig_width": image.shape[3] * 8,
            "orig_height": image.shape[2] * 8,
            "target_width": image.shape[3] * 8,
            "target_height": image.shape[2] * 8,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "crop_coords_top": params.crop_coords_top,
            "crop_coords_left": params.crop_coords_left,
            "aesthetic_score": params.aesthetic_score,
            "negative_aesthetic_score": params.negative_aesthetic_score,
        }

        return do_img2img(
            image,
            self.model,
            sampler,
            value_dict,
            samples,
            skip_encode=True,
            return_latents=return_latents,
            filter=None,
        )


def get_guider_config(params):
    if params.guider == "IdentityGuider":
        guider_config = {
            "target": "nemo.collections.multimodal.modules.stable_diffusion.diffusionmodules.guiders.IdentityGuider"
        }
    elif params.guider == "VanillaCFG":
        scale = params.scale

        thresholder = params.thresholder

        if thresholder == "None":
            dyn_thresh_config = {
                "target": "nemo.collections.multimodal.modules.stable_diffusion.diffusionmodules.sampling_utils.NoDynamicThresholding"
            }
        else:
            raise NotImplementedError

        guider_config = {
            "target": "nemo.collections.multimodal.modules.stable_diffusion.diffusionmodules.guiders.VanillaCFG",
            "params": {"scale": scale, "dyn_thresh_config": dyn_thresh_config},
        }
    else:
        raise NotImplementedError
    return guider_config


def get_discretization_config(params):
    if params.discretization == "LegacyDDPMDiscretization":
        discretization_config = {
            "target": "nemo.collections.multimodal.modules.stable_diffusion.diffusionmodules.discretizer.LegacyDDPMDiscretization",
        }
    elif params.discretization == "EDMDiscretization":
        discretization_config = {
            "target": "nemo.collections.multimodal.modules.stable_diffusion.diffusionmodules.discretizer.EDMDiscretization",
            "params": {
                "sigma_min": params.sigma_min,
                "sigma_max": params.sigma_max,
                "rho": params.rho,
            },
        }
    else:
        raise ValueError(f"unknown discretization {params.discretization}")
    return discretization_config



def get_sampler_config(params):
    discretization_config = get_discretization_config(params)
    guider_config = get_guider_config(params)
    sampler = None
    if params.sampler == "EulerEDMSampler":
        return EulerEDMSampler(
            num_steps=params.steps,
            discretization_config=discretization_config,
            guider_config=guider_config,
            s_churn=params.s_churn,
            s_tmin=params.s_tmin,
            s_tmax=params.s_tmax,
            s_noise=params.s_noise,
            verbose=True,
        )
    if params.sampler == "HeunEDMSampler":
        return HeunEDMSampler(
            num_steps=params.steps,
            discretization_config=discretization_config,
            guider_config=guider_config,
            s_churn=params.s_churn,
            s_tmin=params.s_tmin,
            s_tmax=params.s_tmax,
            s_noise=params.s_noise,
            verbose=True,
        )
    if params.sampler == "EulerAncestralSampler":
        return EulerAncestralSampler(
            num_steps=params.steps,
            discretization_config=discretization_config,
            guider_config=guider_config,
            eta=params.eta,
            s_noise=params.s_noise,
            verbose=True,
        )
    if params.sampler == "DPMPP2SAncestralSampler":
        return DPMPP2SAncestralSampler(
            num_steps=params.steps,
            discretization_config=discretization_config,
            guider_config=guider_config,
            eta=params.eta,
            s_noise=params.s_noise,
            verbose=True,
        )
    if params.sampler == "DPMPP2MSampler":
        return DPMPP2MSampler(
            num_steps=params.steps,
            discretization_config=discretization_config,
            guider_config=guider_config,
            verbose=True,
        )
    if params.sampler == "LinearMultistepSampler":
        return LinearMultistepSampler(
            num_steps=params.steps,
            discretization_config=discretization_config,
            guider_config=guider_config,
            order=params.order,
            verbose=True,
        )

    raise ValueError(f"unknown sampler {params.sampler}!")


