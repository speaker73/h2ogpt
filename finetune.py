import os
import pathlib
import random
import shutil
import subprocess
import sys
import time
from typing import List
import fire

import torch
from datasets import load_dataset
import transformers

from peft import (
    prepare_model_for_int8_training,
    LoraConfig,
    get_peft_model,
    get_peft_model_state_dict,
    set_peft_model_state_dict,
)


def train(
        save_code: bool = False,
        run_id: int = random.randint(0, 2 ** 31),
        # model/data params
        base_model: str = 'decapoda-research/llama-7b-hf',
        data_path: str = "./alpaca_data_cleaned.json",
        llama_type: bool = True,
        output_dir: str = "./lora-alpaca",
        # training hyperparams
        batch_size: int = 128,
        micro_batch_size: int = 4,
        num_epochs: int = 3,
        learning_rate: float = 3e-4,
        cutoff_len: int = 256,
        val_set_size: int = 2000,
        # lora hyperparams
        lora_r: int = 8,
        lora_alpha: int = 16,
        lora_dropout: float = 0.05,
        lora_target_modules: List[str] = [
            "q_proj",
            "v_proj",
        ],
        # llm hyperparams
        train_on_inputs: bool = True,  # if False, masks out inputs in loss
        group_by_length: bool = False,  # faster, but produces an odd training loss curve
        resume_from_checkpoint: str = None,  # either training checkpoint or final adapter
        prompt_type: int = 0,
        # torch training params
        ddp: bool = True,  # set to False if OOM with True, for multi-GPU model parallelism
):
    if save_code:
        copy_code(run_id)
    print(
        f"Training Alpaca-LoRA model with params:\n"
        f"base_model: {base_model}\n"
        f"data_path: {data_path}\n"
        f"output_dir: {output_dir}\n"
        f"batch_size: {batch_size}\n"
        f"micro_batch_size: {micro_batch_size}\n"
        f"num_epochs: {num_epochs}\n"
        f"learning_rate: {learning_rate}\n"
        f"cutoff_len: {cutoff_len}\n"
        f"val_set_size: {val_set_size}\n"
        f"lora_r: {lora_r}\n"
        f"lora_alpha: {lora_alpha}\n"
        f"lora_dropout: {lora_dropout}\n"
        f"lora_target_modules: {lora_target_modules}\n"
        f"train_on_inputs: {train_on_inputs}\n"
        f"group_by_length: {group_by_length}\n"
        f"resume_from_checkpoint: {resume_from_checkpoint}\n"
        f"prompt_type: {prompt_type}\n"
        f"ddp: {ddp}\n"
    )
    assert (
        base_model
    ), "Please specify a --base_model, e.g. --base_model='decapoda-research/llama-7b-hf'"
    gradient_accumulation_steps = batch_size // micro_batch_size

    device_map = "auto"
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    gpus = max(world_size, torch.cuda.device_count())
    max_memory = None
    if gpus > 1:
        if ddp:
            print("data parallel")
            device_map = {"": int(os.environ.get("LOCAL_RANK") or 0)}
            gradient_accumulation_steps = gradient_accumulation_steps // world_size
        else:
            free_in_GB = int(min(torch.cuda.mem_get_info()) / 1024 ** 3)
            max_memory = f"{free_in_GB - 2}GB"
            max_memory = {i: max_memory for i in range(gpus)}
            print("world_size: %d" % world_size)
            print("num_gpus: %d" % gpus)
            print("max mem: %s" % max_memory)

    model_loader, tokenizer_loader = get_loaders(llama_type=llama_type)

    model = model_loader.from_pretrained(
        base_model,
        load_in_8bit=True,
        device_map=device_map,
        max_memory=max_memory,
    )
    if gpus > 1:
        if not ddp:
            print("model parallel")
            model.is_parallelizable = True
            model.model_parallel = True

    tokenizer = tokenizer_loader.from_pretrained(base_model)

    tokenizer.pad_token_id = 0  # unk. we want this to be different from the eos token
    tokenizer.padding_side = "left"  # Allow batched inference

    def tokenize(prompt, add_eos_token=True):
        # there's probably a way to do this with the tokenizer settings
        # but again, gotta move fast
        result = tokenizer(
            prompt,
            truncation=True,
            max_length=cutoff_len,
            padding=False,
            return_tensors=None,
        )
        if (
                result["input_ids"][-1] != tokenizer.eos_token_id
                and len(result["input_ids"]) < cutoff_len
                and add_eos_token
        ):
            result["input_ids"].append(tokenizer.eos_token_id)
            result["attention_mask"].append(1)

        result["labels"] = result["input_ids"].copy()

        return result

    def generate_and_tokenize_prompt(data_point):
        full_prompt, _ = generate_prompt(data_point, prompt_type)
        tokenized_full_prompt = tokenize(full_prompt)
        if not train_on_inputs:
            user_prompt, _ = generate_prompt({**data_point, "output": ""}, prompt_type)
            tokenized_user_prompt = tokenize(user_prompt, add_eos_token=False)
            user_prompt_len = len(tokenized_user_prompt["input_ids"])

            tokenized_full_prompt["labels"] = [
                                                  -100
                                              ] * user_prompt_len + tokenized_full_prompt["labels"][
                                                                    user_prompt_len:
                                                                    ]  # could be sped up, probably
        return tokenized_full_prompt

    model = prepare_model_for_int8_training(model)

    config = LoraConfig(
        r=lora_r,
        lora_alpha=lora_alpha,
        target_modules=lora_target_modules,
        lora_dropout=lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, config)

    data = load_dataset("json", data_files=data_path)

    if resume_from_checkpoint:
        # Check the available weights and load them
        checkpoint_name = os.path.join(
            resume_from_checkpoint, "pytorch_model.bin"
        )  # Full checkpoint
        if not os.path.exists(checkpoint_name):
            checkpoint_name = os.path.join(
                resume_from_checkpoint, "adapter_model.bin"
            )  # only LoRA model - LoRA config above has to fit
            resume_from_checkpoint = False  # So the trainer won't try loading its state
        # The two files above have a different name depending on how they were saved, but are actually the same.
        if os.path.exists(checkpoint_name):
            print(f"Restarting from {checkpoint_name}")
            adapters_weights = torch.load(checkpoint_name)
            model = set_peft_model_state_dict(model, adapters_weights)
        else:
            print(f"Checkpoint {checkpoint_name} not found")

    model.print_trainable_parameters()  # Be more transparent about the % of trainable params.

    if val_set_size > 0:
        train_val = data["train"].train_test_split(
            test_size=val_set_size, shuffle=True, seed=42
        )
        train_data = train_val["train"].shuffle().map(generate_and_tokenize_prompt)
        val_data = train_val["test"].shuffle().map(generate_and_tokenize_prompt)
    else:
        train_data = data["train"].shuffle().map(generate_and_tokenize_prompt)
        val_data = None

    trainer = transformers.Trainer(
        model=model,
        train_dataset=train_data,
        eval_dataset=val_data,
        args=transformers.TrainingArguments(
            per_device_train_batch_size=micro_batch_size,
            gradient_accumulation_steps=gradient_accumulation_steps,
            warmup_steps=100,
            num_train_epochs=num_epochs,
            learning_rate=learning_rate,
            fp16=True,
            logging_steps=10,
            evaluation_strategy="steps" if val_set_size > 0 else "no",
            save_strategy="steps",
            eval_steps=200 if val_set_size > 0 else None,
            save_steps=200,
            output_dir=output_dir,
            save_total_limit=3,
            load_best_model_at_end=True if val_set_size > 0 else False,
            ddp_find_unused_parameters=False if ddp else None,
            group_by_length=group_by_length,
        ),
        data_collator=transformers.DataCollatorForSeq2Seq(
            tokenizer, pad_to_multiple_of=8, return_tensors="pt", padding=True
        ),
    )
    model.config.use_cache = False

    old_state_dict = model.state_dict
    model.state_dict = (
        lambda self, *_, **__: get_peft_model_state_dict(self, old_state_dict())
    ).__get__(model, type(model))

    if torch.__version__ >= "2" and sys.platform != "win32":
        model = torch.compile(model)

    if gpus > 1 and not ddp:
        assert trainer.is_model_parallel
    else:
        assert not trainer.is_model_parallel
    trainer.train(resume_from_checkpoint=resume_from_checkpoint)

    model.save_pretrained(output_dir)

    print("\n If there's a warning about missing keys above, please disregard :)")


def get_loaders(llama_type):
    if llama_type:
        assert (
                "LlamaTokenizer" in transformers._import_structure["models.llama"]
        ), "LLaMA is now in HuggingFace's main branch.\nPlease reinstall it: pip uninstall transformers && pip install git+https://github.com/huggingface/transformers.git"
        from transformers import LlamaForCausalLM, LlamaTokenizer

        model_loader = LlamaForCausalLM
        tokenizer_loader = LlamaTokenizer
    else:
        from transformers import AutoTokenizer, AutoModelForCausalLM

        model_loader = AutoModelForCausalLM
        tokenizer_loader = AutoTokenizer
    return model_loader, tokenizer_loader


def get_githash():
    try:
        githash = subprocess.run(['git', 'rev-parse', 'HEAD'], stdout=subprocess.PIPE).stdout.decode('utf-8')[0:-1]
    except:
        githash = ''
    return githash


def copy_code(run_id):
    """
    copy code to track changes
    :param run_id:
    :return:
    """
    rnd_num = str(random.randint(0, 2 ** 31))
    run_id = 'run_' + str(run_id)
    os.makedirs(run_id, exist_ok=True)
    me_full = os.path.join(pathlib.Path(__file__).parent.resolve(), __file__)
    me_file = os.path.basename(__file__)
    new_me = os.path.join(run_id, me_file + '_' + get_githash())
    if os.path.isfile(new_me):
        new_me = os.path.join(run_id, me_file + '_' + get_githash() + '_' + rnd_num)
        shutil.copy(me_full, new_me)
    else:
        shutil.copy(me_full, new_me)


def get_prompt(prompt_type):
    if prompt_type == -1:
        promptA = promptB = PreInstruct = PreInput = PreResponse = ''
    elif prompt_type == 0:
        promptA = 'Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.\n'
        promptB = 'Below is an instruction that describes a task. Write a response that appropriately completes the request.\n'

        PreInstruct = """
### Instruction:
"""

        PreInput = """
### Input:
"""

        PreResponse = """
### Response:
"""
    elif prompt_type == 1:
        promptA = 'Write a detailed high-quality, accurate, fair, Response with about 100 words by following the Instruction as applied on the Input.\n'
        promptB = 'Write a detailed high-quality, accurate, fair, Response with about 100 words by following the Instruction.\n'

        PreInstruct = """
### Instruction:
"""

        PreInput = """
### Input:
"""

        PreResponse = """
### Response:
"""
    elif prompt_type == 2:
        cur_date = time.strftime('%Y-%m-%d')
        cur_time = time.strftime('%H:%M:%S %p %Z')

        PRE_PROMPT = """\
Current Date: {}
Current Time: {}

"""

        preprompt = PRE_PROMPT.format(cur_date, cur_time)

        promptA = '%s<human>: ' % preprompt
        promptB = '%s<human>: ' % preprompt

        PreInstruct = ""

        PreInput = None

        PreResponse = "<bot>: "
    elif prompt_type == 3:
        promptA = ''
        promptB = 'Answer the following Driverless AI question.\n'

        PreInstruct = """
### Driverless AI frequently asked question:
"""

        PreInput = None

        PreResponse = """
### Driverless AI documentation answer:
"""
    else:
        raise RuntimeError("No such prompt_type=%s" % prompt_type)

    return promptA, promptB, PreInstruct, PreInput, PreResponse


def generate_prompt(data_point, prompt_type):
    instruction = data_point.get('instruction')
    input = data_point.get('input')
    output = data_point.get('output')
    promptA, promptB, PreInstruct, PreInput, PreResponse = get_prompt(prompt_type)

    prompt = ''

    if input and promptA:
        prompt += f"""{promptA}"""
    elif promptB:
        prompt += f"""{promptB}"""

    if instruction and PreInstruct is not None and input and PreInput is not None:
        prompt += f"""{PreInstruct}{instruction}{PreInput}{input}
"""
    elif instruction and input and PreInstruct is None and PreInput is not None:
        prompt += f"""{PreInput}{instruction}
{input}
"""
    elif input and instruction and PreInput is None and PreInstruct is not None:
        prompt += f"""{PreInstruct}{instruction}
{input}
"""
    elif instruction and PreInstruct is not None:
        prompt += f"""{PreInstruct}{instruction}
"""
    elif input and PreInput is not None:
        prompt += f"""{PreInput}{input}
"""
    elif input and instruction and PreInput is not None:
        prompt += f"""{PreInput}{instruction}{input}
"""
    elif input and instruction and PreInstruct is not None:
        prompt += f"""{PreInstruct}{instruction}{input}
"""
    elif input and instruction:
        prompt += f"""{PreInput}{instruction}{input}
"""
    elif input:
        prompt += f"""{input}
"""
    elif instruction:
        prompt += f"""{instruction}
"""

    if PreResponse is not None:
        prompt += f"""{PreResponse}"""
        clean_response = PreResponse.strip()
    else:
        clean_response = ''

    if output:
        prompt += f"""{output}"""

    return prompt, clean_response


example_data_point0 = dict(instruction="Summarize",
                           input="Ducks eat seeds by the lake, then swim in the lake where fish eat small animals.",
                           output="Ducks eat and swim at the lake.")

example_data_point1 = dict(instruction="Who is smarter, Einstein or Newton?",
                           output="Einstein.")

example_data_point2 = dict(input="Who is smarter, Einstein or Newton?",
                           output="Einstein.")

example_data_points = [example_data_point0, example_data_point1, example_data_point2]


def test_train_prompt(prompt_type=0, data_point=0):
    example_data_point = example_data_points[data_point]
    return generate_prompt(example_data_point, prompt_type)


if __name__ == "__main__":
    print("""
    Example run on 4 GPUs:
    WORLD_SIZE=4 CUDA_VISIBLE_DEVICES="0,1,2,3" torchrun --nproc_per_node=4 --master_port=1234 finetune.py --llama_type=True --base_model='decapoda-research/llama-7b-hf' --output_dir='lora_alpaca_7B' --data_path=alpaca_data_cleaned.json --run_id=0 &> 0.log
    WORLD_SIZE=4 CUDA_VISIBLE_DEVICES="0,1,2,3" torchrun --nproc_per_node=4 --master_port=1234 finetune.py --llama_type=True --base_model='decapoda-research/llama-30b-hf' --output_dir='lora_alpaca_30B' --data_path=alpaca_data_cleaned.json --batch_size=16 --micro_batch_size=1 --run_id=1 --save_code=True &> 1.log
    WORLD_SIZE=4 CUDA_VISIBLE_DEVICES="0,1,2,3" torchrun --nproc_per_node=4 --master_port=1234 finetune.py --base_model='EleutherAI/gpt-j-6B' --output_dir='lora_alpaca_6B' --data_path=alpaca_data_cleaned.json --run_id=2 &> 2.log

    WORLD_SIZE=4 CUDA_VISIBLE_DEVICES="0,1,2,3" torchrun --nproc_per_node=4 --master_port=1234 finetune.py --base_model='EleutherAI/gpt-neox-20b' --output_dir='lora_alpaca_20B' --data_path=alpaca_data_cleaned.json --lora_target_modules='["query_key_value"]' --run_id=8 --batch_size=16 --micro_batch_size=4 &> 8.log

    """, flush=True)
    fire.Fire(train)
