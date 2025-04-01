# Copyright 2025 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
# LoRA
python trl/scripts/sft.py \
    --model_name_or_path Qwen/Qwen2-0.5B \
    --dataset_name trl-lib/Capybara \
    --learning_rate 2.0e-4 \
    --num_train_epochs 1 \
    --packing \
    --per_device_train_batch_size 2 \
    --gradient_accumulation_steps 8 \
    --gradient_checkpointing \
    --logging_steps 25 \
    --eval_strategy steps \
    --eval_steps 100 \
    --use_peft \
    --lora_r 32 \
    --lora_alpha 16 \
    --output_dir Qwen2-0.5B-SFT \
    --push_to_hub

# Full training
python trl/scripts/sft.py \
    --model_name_or_path Qwen/Qwen2-0.5B \
    --dataset_name trl-lib/Capybara \
    --learning_rate 2.0e-5 \
    --num_train_epochs 1 \
    --packing \
    --per_device_train_batch_size 2 \
    --gradient_accumulation_steps 8 \
    --gradient_checkpointing \
    --logging_steps 25 \
    --eval_strategy steps \
    --eval_steps 100 \
    --output_dir Qwen2-0.5B-SFT \
    --push_to_hub

CUDA_VISIBLE_DEVICES=0,1 nohup python sft.py \
    --model_name_or_path turboderp/Qwama-0.5B-Instruct \
    --dataset_name Skywork/Skywork-Reward-Preference-80K-v0.1 \
    --learning_rate 2.0e-5 \
    --num_train_epochs 1 \
    --packing \
    --per_device_train_batch_size 2 \
    --gradient_accumulation_steps 16 \
    --gradient_checkpointing False \
    --logging_steps 25 \
    --output_dir qwama-0.5b-skywork-pref-sft-chosen-trl-v3 \
    &> qwama-0.5b-skywork-pref-sft-chosen-trl-v3.log &

CUDA_VISIBLE_DEVICES=1,0 nohup python sft.py \
    --model_name_or_path lblaoke/qwama-0.5b-skywork-pref-sft-rejected-trl-v3 \
    --dataset_name Skywork/Skywork-Reward-Preference-80K-v0.1 \
    --learning_rate 2.0e-5 \
    --num_train_epochs 1 \
    --packing \
    --per_device_train_batch_size 2 \
    --gradient_accumulation_steps 16 \
    --gradient_checkpointing False \
    --logging_steps 25 \
    --output_dir qwama-0.5b-skywork-pref-sft-rejected-chosen-trl-v3 \
    &> qwama-0.5b-skywork-pref-sft-rejected-chosen-trl-v3.log &

huggingface-cli upload-large-folder --repo-type=model lblaoke/xxx ./xxx
"""

import argparse

import torch

from datasets import load_dataset
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
from transformers.models.auto.modeling_auto import MODEL_FOR_IMAGE_TEXT_TO_TEXT_MAPPING_NAMES

from trl import (
    ModelConfig,
    ScriptArguments,
    SFTConfig,
    SFTTrainer,
    TrlParser,
    get_kbit_device_map,
    get_peft_config,
    get_quantization_config,
)


def main(script_args, training_args, model_args):
    ################
    # Model init kwargs & Tokenizer
    ################
    quantization_config = get_quantization_config(model_args)
    model_kwargs = dict(
        revision=model_args.model_revision,
        trust_remote_code=model_args.trust_remote_code,
        attn_implementation='flash_attention_2',
        torch_dtype=torch.bfloat16,
        # use_cache=False if training_args.gradient_checkpointing else True,
        use_cache=False,
        device_map=get_kbit_device_map() if quantization_config is not None else 'auto',
        quantization_config=quantization_config,
    )

    # Create model
    config = AutoConfig.from_pretrained(model_args.model_name_or_path)
    valid_image_text_architectures = MODEL_FOR_IMAGE_TEXT_TO_TEXT_MAPPING_NAMES.values()

    if config.architectures and any(arch in valid_image_text_architectures for arch in config.architectures):
        from transformers import AutoModelForImageTextToText

        model_kwargs.pop("use_cache", None)  # Image models do not support cache
        model = AutoModelForImageTextToText.from_pretrained(model_args.model_name_or_path, **model_kwargs)
    else:
        model = AutoModelForCausalLM.from_pretrained(model_args.model_name_or_path, **model_kwargs)

    # Create tokenizer
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.model_name_or_path, trust_remote_code=model_args.trust_remote_code, use_fast=True
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    ################
    # Dataset
    ################
    dataset = load_dataset(script_args.dataset_name, name=script_args.dataset_config)

    def formatting_prompts_func(example):
        output_texts = []

        if len(example['chosen']) == 2:
            output_texts.append(f"### User: {example['chosen'][0]['content']}\n\n### Assistant: {example['chosen'][1]['content']}")
        else:
            for i in range(len(example['chosen'])):
                text = f"### User: {example['chosen'][i][0]['content']}\n\n### Assistant: {example['chosen'][i][1]['content']}"
                output_texts.append(text)

        return output_texts

    ################
    # Training
    ################
    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset[script_args.dataset_train_split],
        processing_class=tokenizer,
        peft_config=get_peft_config(model_args),
        formatting_func=formatting_prompts_func,
    )

    trainer.train()

    # Save and push to hub
    trainer.save_model(training_args.output_dir)
    if training_args.push_to_hub:
        trainer.push_to_hub(dataset_name=script_args.dataset_name)


def make_parser(subparsers: argparse._SubParsersAction = None):
    dataclass_types = (ScriptArguments, SFTConfig, ModelConfig)
    if subparsers is not None:
        parser = subparsers.add_parser("sft", help="Run the SFT training script", dataclass_types=dataclass_types)
    else:
        parser = TrlParser(dataclass_types)
    return parser


if __name__ == "__main__":
    parser = make_parser()
    script_args, training_args, model_args = parser.parse_args_and_config()
    training_args.report_to = []
    main(script_args, training_args, model_args)
