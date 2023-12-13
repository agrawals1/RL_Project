"""
This script run a simple agent in a BabyAI GoTo-Local environment.
"""
import os
import distutils
import csv
import json
from collections import OrderedDict

import logging

logger = logging.getLogger(__name__)
from colorama import Fore

import time

import numpy as np
import torch
import gym
import torch.nn.functional as F
from torch.distributions import Categorical
import os
import babyai_text
import babyai.utils as utils
from babyai.paral_env_simple import ParallelEnv

from agents.drrn.drrn import DRRNAgent
from agents.ppo.llm_ppo_agent import LLMPPOAgent

from lamorel import Caller, lamorel_init
from lamorel import BaseUpdater, BaseModuleFunction
os.environ["MASTER_ADDR"] = "127.0.0.1"
os.environ["MASTER_PORT"] = "5555"
os.environ["RANK"] = "0"
os.environ["LOCAL_RANK"] = "0"
os.environ["WORLD_SIZE"] = "1"
os.environ["CUDA_VISIBLE_DEVICES"] = "0,1"
lamorel_init()

import hydra

from accelerate import Accelerator

accelerator = Accelerator()

def reward_function(subgoal_proba=None, reward=None, policy_value=None, llm_0=None):
    if reward > 0:
        return [20 * reward, 0]
    else:
        return [0, 0]


def reward_function_shapped(subgoal_proba=None, reward=None, policy_value=None, llm_0=None):
    if reward > 0:
        return [20 * reward - np.log(subgoal_proba / policy_value), -np.log(subgoal_proba / policy_value)]
    else:
        return [0 - np.log(subgoal_proba / policy_value), 0 - np.log(subgoal_proba / policy_value)]

class LogScoringModuleFn(BaseModuleFunction):
    def __init__(self, model_type):
        super().__init__()
        self._model_type = model_type
        self._pad_token = 0
        

    def initialize(self):
        pass

    def forward(self, forward_outputs, minibatch, tokenized_contexts, **kwargs):
        if self._model_type == "causal":  # hence input should be removed from result
            logits = forward_outputs["logits"][:, len(tokenized_contexts["input_ids"]) - 1:-1, :]
            output_tokens = minibatch["input_ids"][:, len(tokenized_contexts["input_ids"]):]
        else:
            logits = forward_outputs["logits"][:, :-1, :]  # skip </s> token appended by tokenizer
            output_tokens = minibatch["decoder_input_ids"][:, 1:]  # skip pad token

        tokens_logprobs = \
            torch.gather(logits, 2, output_tokens[:, :, None]).squeeze(-1).to(torch.float32)  # filter with sequence tokens

        # Compute mask to assign probability 1 to padding tokens
        self.device = "cuda:1"
        mask = torch.ones(tokens_logprobs.shape, dtype=torch.bool, device=self.device)
        for i, _output in enumerate(output_tokens):
            for j, _token in enumerate(_output):
                if _token != self._pad_token:
                    mask[i, j] = False
        masked_token_probs = tokens_logprobs.masked_fill(mask, 1.0)  # apply mask
        minibatch_probs = masked_token_probs.sum(-1)  # compute final sequences' probability

        return minibatch_probs.cpu()

class ValueHeadModuleFn(BaseModuleFunction):
    def __init__(self, model_type):
        super().__init__()
        self._model_type = model_type

    def initialize(self):
        llm_hidden_size = self.llm_config.to_dict()[self.llm_config.attribute_map['hidden_size']]
        self.device = "cuda:1"
        self.value_head_op = torch.nn.Sequential(
            torch.nn.Linear(llm_hidden_size, 1024),
            torch.nn.Sigmoid(),
            torch.nn.Linear(1024, 1024),
            torch.nn.Sigmoid(),
            torch.nn.Linear(1024, 1),
        ).to(self.device)

    def forward(self, forward_outputs, minibatch, tokenized_contexts, **kwargs):
        if self._model_type == "causal":
            model_head = forward_outputs['hidden_states'][-1][:, len(tokenized_contexts["input_ids"]) - 1, :]
        else:
            model_head = forward_outputs["decoder_hidden_states"][-1][:, 0, :]
        self.device = "cuda:1"
        value = self.value_head_op(model_head.to(torch.float32).to(self.device))
        return value.cpu()

class ActionHeadsModuleFn(BaseModuleFunction):
    def __init__(self, model_type, action_space_size):
        super().__init__()
        self._model_type = model_type
        self._action_space_size = action_space_size

    def initialize(self):
        llm_hidden_size = self.llm_config.to_dict()[self.llm_config.attribute_map['hidden_size']]
        self.device = "cuda:1"
        self.action_heads_op = torch.nn.Sequential(
            torch.nn.Linear(llm_hidden_size, 1024),
            torch.nn.Sigmoid(),
            torch.nn.Linear(1024, 1024),
            torch.nn.Sigmoid(),
            torch.nn.Linear(1024, self._action_space_size)
        ).to(self.device)

    def forward(self, forward_outputs, minibatch, tokenized_contexts, **kwargs):
        # Get encoder's representation
        if self._model_type == "causal":
            model_head = forward_outputs['hidden_states'][-1][0, len(tokenized_contexts["input_ids"]) - 1, :]
        else:
            model_head = forward_outputs["decoder_hidden_states"][-1][:, 0, :]
        self.device = "cuda:1"
        actions_score = self.action_heads_op(model_head.to(self.device))
        return actions_score.cpu()


class PPOUpdater(BaseUpdater):
    def get_test_prompts(self, subgoals, template_test):
        head_prompt = "Possible action of the agent:"
        for sg in subgoals:
            head_prompt += " {},".format(sg)
        head_prompt = head_prompt[:-1]

        if template_test == 1:
            # expected answers: go forward, turn left, turn left, toggle
            templated_prompts = [
                ' \n Goal of the agent: go to the green ball \n Observation 0: A wall 2 step left, A purple key 1 step left and 2 steps forward, A yellow key 1 step left and 1 step forward, A green ball 3 steps forward, A grey ball 1 step right and 5 steps forward, A green key 1 step right and 2 steps forward, A grey ball 1 step right and 1 step forward, A green key 2 steps right and 4 steps forward, A red box 2 steps right and 2 steps forward, \n Action 0: ',
                ' \n Goal of the agent: go to the green ball \n Observation 0: A wall 2 step left, A purple key 1 step left and 2 steps forward, A yellow key 1 step left and 1 step forward, A green ball 3 steps forward, A grey ball 1 step right and 5 steps forward, A green key 1 step right and 2 steps forward, A grey ball 1 step right and 1 step forward, A green key 2 steps right and 4 steps forward, A red box 2 steps right and 2 steps forward, \n Action 0: go forward \n Observation 1: A purple key 1 step left and 1 step forward, A yellow key 1 step left, A green ball 2 steps forward, A grey ball 1 step right and 4 steps forward, A green key 1 step right and 1 step forward, A grey ball 1 step right, A green key 2 steps right and 3 steps forward, A red box 2 steps right and 1 step forward, \n Action 1: turn right \n Observation 2: A wall 2 step right, A green key 3 steps left and 2 steps forward, A green ball 2 steps left, A red box 1 step left and 2 steps forward, A green key 1 step left and 1 step forward, A grey ball 1 step forward, \n Action 2: ',
                ' \n Goal of the agent: open the purple door \n Observation 0: You see a wall 3 steps forward, You see a wall 3 steps left, You see a yellow key 1 step right and 1 step forward, You see a locked purple door 2 steps right and 3 steps forward, You see a purple ball 3 steps right and 1 step forward, You see a green box 3 steps right, You see a purple key 2 steps left \n Action 0: ',
                ' \n Goal of the agent: open the purple door \n Observation 0: You see a wall 3 steps forward, You see a wall 3 steps left, You see a yellow key 1 step right and 1 step forward, You see a locked purple door 2 steps right and 3 steps forward, You see a purple ball 3 steps right and 1 step forward, You see a green box 3 steps right, You see a purple key 2 steps left \n Action 0: turn left \n Observation 1: You see a wall 3 steps forward, You see a wall 3 steps right, You see a purple key 2 steps forward \n Action 1: go forward \n Observation 2: You see a wall 2 steps forward, You see a wall 3 steps right, You see a purple key 1 step forward \n Action 2: ',
                ' \n Goal of the agent: open the purple door \n Observation 0: You carry a purple key, You see a wall 3 steps forward, You see a wall 5 steps left, You see a yellow key 1 step left and 1 step forward, You see a locked purple door 3 steps forward, You see a purple ball 1 step right and 1 step forward, You see a green box 1 step right \n Action 0: go forward \n Observation 1: You carry a purple key, You see a wall 2 steps forward, You see a wall 5 steps left, You see a yellow key 1 step left, You see a locked purple door 2 steps forward, You see a purple ball 1 step right \n Action 1: go forward \n Observation 2: You carry a purple key, You see a wall 1 step forward, You see a wall 5 steps left, You see a locked purple door 1 step forward \n Action 2: ',
                ' \n Goal of the agent: pick up green box \n Observation 0: You see a wall 2 steps forward, You see a wall 2 steps left, You see a yellow ball 1 step left and 1 step forward, You see a green box 2 steps right \n Action 0: ',
                ' \n Goal of the agent: pick up green box \n Observation 0: You see a wall 2 steps forward, You see a wall 2 steps left, You see a yellow ball 1 step left and 1 step forward, You see a green box 2 steps right \n Action 0: turn right \n Observation 1: You see a wall 2 steps left, You see a blue key 1 step right, You see a red ball 2 steps right and 1 step forward, You see a green box 2 steps forward \n Action 1: go forward \n Observation 2: You see a wall 2 steps left, You see a red ball 2 steps right, You see a green box 1 step forward \n Action 2: ',
                ' \n Goal of the agent: put blue ball next to red box \n Observation 0: You carry a blue ball, You see a wall 5 steps forward, You see a wall 2 steps left, You see a grey key 1 step right and 2 steps forward, You see a red box 3 steps forward \n Action 0: go forward \n Observation 1: You carry a blue ball, You see a wall 4 steps forward, You see a wall 2 steps left, You see a grey key 1 step right and 1 step forward, You see a red box 2 steps forward \n Action 1: ',
                ' \n Goal of the agent: pick up the blue ball then go to the red box \n Observation 0: You see a wall 3 steps forward, You see a wall 4 steps right, You see a purple key 2 steps forward, You see a red box 2 steps right, You see a blue ball 2 steps left \n Action 0: ',
                ' \n Goal of the agent: go to the red box after you pick up the blue ball \n Observation 0: You see a wall 3 steps forward, You see a wall 4 steps right, You see a purple key 2 steps forward, You see a red box 2 steps right, You see a blue ball 2 steps left \n Action 0: ',
                ' \n Goal of the agent: pick up the green key then pick up the the red box \n Observation 0: You carry a green key, You see a wall 4 steps forward, You see a wall 4 steps left, You see a red box 1 step left, You see a purple ball 2 steps left and 1 step forward \n Action 0:  ']
        elif template_test == 2:
            # expected answers: go forward, turn left
            templated_prompts = [
                ' \n Goal of the agent: go to the green ball \n Observation 0: A wall 2 step left, A purple key 1 step left and 2 steps forward, A yellow key 1 step left and 1 step forward, A green ball 3 steps forward, A grey ball 1 step right and 5 steps forward, A green key 1 step right and 2 steps forward, A grey ball 1 step right and 1 step forward, A green key 2 steps right and 4 steps forward, A red box 2 steps right and 2 steps forward, \n Action 0: ',
                ' \n Goal of the agent: go to the green ball \n Observation 0: A wall 2 step left, A purple key 1 step left and 2 steps forward, A yellow key 1 step left and 1 step forward, A green ball 3 steps forward, A grey ball 1 step right and 5 steps forward, A green key 1 step right and 2 steps forward, A grey ball 1 step right and 1 step forward, A green key 2 steps right and 4 steps forward, A red box 2 steps right and 2 steps forward, \n Action 0: go forward \n Observation 1: A purple key 1 step left and 1 step forward, A yellow key 1 step left, A green ball 2 steps forward, A grey ball 1 step right and 4 steps forward, A green key 1 step right and 1 step forward, A grey ball 1 step right, A green key 2 steps right and 3 steps forward, A red box 2 steps right and 1 step forward, \n Action 1: turn right \n Observation 2: A wall 2 step right, A green key 3 steps left and 2 steps forward, A green ball 2 steps left, A red box 1 step left and 2 steps forward, A green key 1 step left and 1 step forward, A grey ball 1 step forward, \n Action 2: ']

        for j in range(len(templated_prompts)):
            templated_prompts[j] = head_prompt + templated_prompts[j]
        return templated_prompts

    def perform_update(self, contexts, candidates, _current_batch_ids, **kwargs):
        # Initialize model if asked + optimizer
        if not hasattr(self, 'optimizer') and "load_fine_tuned_version" not in kwargs:
            self.optimizer = torch.optim.Adam(self._llm_module.parameters(), kwargs["lr"],
                                                  (kwargs["beta1"], kwargs["beta2"]),
                                                  eps=kwargs["adam_eps"])

        if "load_embedding" in kwargs or "load_fine_tuned_version" in kwargs or "save_first_last" in kwargs:
            # If asked, only do embedding weights loading
            if "load_embedding" in kwargs and kwargs["load_embedding"] and not hasattr(self, "is_embedding_loaded"):
                pretrained_weights = torch.load(kwargs["llm_path"] + "/pytorch_model.bin")
                state_dict = OrderedDict({
                    k: v for k, v in pretrained_weights.items() if "embed" in k or "shared" in k
                    # Warning: this may fail if the model shares other things than embedding weights
                })
                self._llm_module.module._LLM_model.load_state_dict(state_dict, strict=False)
                self.is_embedding_loaded = True

                torch.save(self._llm_module.state_dict(), kwargs["saving_path_model"] +
                           "/" + kwargs["id_expe"] + "/last/model.checkpoint")
                torch.save(self.optimizer.state_dict(), kwargs["saving_path_model"] +
                           "/" + kwargs["id_expe"] + "/last/optimizer.checkpoint")
            elif "load_fine_tuned_version" in kwargs and kwargs["load_fine_tuned_version"] \
                    and not hasattr(self, "is_loaded"):
                try:
                    self._llm_module.load_state_dict(torch.load(kwargs["saving_path_model"] +
                                                                "/" + kwargs["id_expe"] + "/last/model.checkpoint"))
                    self.optimizer = torch.optim.Adam(self._llm_module.parameters())
                    self.optimizer.load_state_dict(torch.load(
                        kwargs["saving_path_model"] + "/" + kwargs["id_expe"] + "/last/optimizer.checkpoint"))
                    self.is_loaded = True
                except:
                    # The last save has been corrupted for whatever reasons, possibly the program has been forced
                    # to close during the saving => we use the backup
                    self._llm_module.load_state_dict(torch.load(kwargs["saving_path_model"] +
                                                                "/" + kwargs["id_expe"] + "/backup/model.checkpoint"))
                    self.optimizer = torch.optim.Adam(self._llm_module.parameters())
                    self.optimizer.load_state_dict(torch.load(
                        kwargs["saving_path_model"] + "/" + kwargs["id_expe"] + "/backup/optimizer.checkpoint"))
                    self.is_loaded = True

                    dest = kwargs["saving_path_model"] + "/" + kwargs["id_expe"] + "/last"
                    src = kwargs["saving_path_model"] + "/" + kwargs["id_expe"] + "/backup"
                    distutils.dir_util.copy_tree(src, dest)
            elif "save_first_last" in kwargs and kwargs["save_first_last"] \
                    and not hasattr(self, "save_first_last"):
                torch.save(self._llm_module.state_dict(), kwargs["saving_path_model"] +
                           "/" + kwargs["id_expe"] + "/last/model.checkpoint")
                torch.save(self.optimizer.state_dict(), kwargs["saving_path_model"] +
                           "/" + kwargs["id_expe"] + "/last/optimizer.checkpoint")
                self.save_first_last = True

            return {}

        else:
            # save the proba_dist over actions every n updates
            if accelerator.process_index == 1 and kwargs["lm_server_update_first_call"]:
                if kwargs["number_updates"] % 1 == 0 and candidates is not None:
                    prompts = self.get_test_prompts(candidates[0], kwargs['template_test'])
                    subgoals = [candidates[0] for i in range(len(prompts))]

                    # Avoid calling DDP model and get stuck gathering buffers from all LLMs
                    output = self._llm_module.module([kwargs["scoring_module_key"], 'value'],
                                                     contexts=prompts, candidates=subgoals, require_grad=False)
                    scores = torch.stack([_o[kwargs["scoring_module_key"]] for _o in output]).squeeze()
                    proba_dist = list(scores.cpu().numpy().flatten())

                    csv_distrib_path = os.path.join(kwargs["experiment_path"], 'distrib.csv')
                    csv_writer = csv.writer(open(csv_distrib_path, 'a', 1))
                    csv_writer.writerow(proba_dist)

            sb = {}
            for k in ['action', 'value', 'log_prob', 'advantage', 'returnn']:
                sb[k] = kwargs["exps"][k][_current_batch_ids]

            # PPO update
            output = self._llm_module([kwargs["scoring_module_key"], 'value'],
                                      contexts=contexts, candidates=candidates, require_grad=True)
            scores = torch.stack([_o[kwargs["scoring_module_key"]] for _o in output]).squeeze()
            dist = Categorical(logits=scores)

            values = torch.stack([_o["value"][0] for _o in output])
            
            entropy = dist.entropy().mean()
            log_prob = dist.log_prob(sb['action'])
            if len(log_prob.shape) > 1:
                log_prob = log_prob.sum(dim=-1)
            ratio = torch.exp(log_prob - sb['log_prob'])
            surr1 = ratio * sb['advantage']
            surr2 = torch.clamp(ratio, 1.0 - kwargs["clip_eps"], 1.0 + kwargs["clip_eps"]) * sb['advantage']
            policy_loss = -torch.min(surr1, surr2).mean()

            value_clipped = sb['value'] + torch.clamp(values - sb['value'], -kwargs["clip_eps"], kwargs["clip_eps"])
            surr_v1 = (values - sb['returnn']).pow(2)
            surr_v2 = (value_clipped - sb['returnn']).pow(2)
            value_loss = torch.max(surr_v1, surr_v2).mean()

            loss = policy_loss - kwargs["entropy_coef"] * entropy + kwargs["value_loss_coef"] * value_loss

            self.optimizer.zero_grad()
            loss.backward()
            grad_norm = sum(
                p.grad.data.detach().cpu().norm(2) ** 2 for p in self._llm_module.parameters() if
                p.grad is not None) ** 0.5
            torch.nn.utils.clip_grad_norm_(self._llm_module.parameters(), kwargs["max_grad_norm"])
            self.optimizer.step()

            dict_return = {"loss": loss.item(),
                           "entropy": entropy.item(),
                           "policy_loss": policy_loss.item(),
                           "value_loss": value_loss.item(),
                           "grad_norm": grad_norm.item()}

            # save the model every n updates
            if accelerator.process_index == 1 and kwargs["lm_server_update_first_call"]:
                if kwargs["number_updates"] % 1 == 0:
                    # saving the back-up
                    src = kwargs["saving_path_model"] + "/" + kwargs["id_expe"] + "/last"
                    dest = kwargs["saving_path_model"] + "/" + kwargs["id_expe"] + "/backup"
                    distutils.dir_util.copy_tree(src, dest)

                    # saving the last iteration
                    torch.save(self._llm_module.state_dict(), kwargs["saving_path_model"] +
                               "/" + kwargs["id_expe"] + "/last/model.checkpoint")
                    torch.save(self.optimizer.state_dict(), kwargs["saving_path_model"] +
                               "/" + kwargs["id_expe"] + "/last/optimizer.checkpoint")


            return dict_return


def run_agent(args, algo, id_expe):
    header = (["update", "episodes", "frames", "FPS", "duration"]
              + ["return_" + stat for stat in ['mean', 'std', 'min', 'max']]
              + ["success_rate"]
              + ["reshaped_return_" + stat for stat in ['mean', 'std', 'min', 'max']]
              + ["reshaped_return_bonus_" + stat for stat in ['mean', 'std', 'min', 'max']]
              + ["num_frames_" + stat for stat in ['mean', 'std', 'min', 'max']]
              + ["entropy", "policy_loss", "value_loss", "loss", "grad_norm"])

    experiment_path = os.path.join(args.saving_path_logs, id_expe)
    if not os.path.exists(experiment_path):
        os.makedirs(experiment_path)

    csv_path = os.path.join(experiment_path, 'log.csv')
    # we don't buffer data going in the csv log, because we assume
    # that one update will take much longer than one write to the log
    first_created = not os.path.exists(csv_path)
    csv_writer = csv.writer(open(csv_path, 'a', 1))
    if first_created:
        csv_writer.writerow(header)

    # Restore training status
    status_path = os.path.join(experiment_path, 'status.json')
    if os.path.exists(status_path):
        with open(status_path, 'r') as src:
            status = json.load(src)
    else:
        status = {'i': 0,
                  'num_episodes': 0,
                  'num_frames': 0}

    format_str = ("\nUpdate: {} | Episodes Done: {} | Frames Seen: {:06} | FPS: {:04.0f} | Ellapsed: {}\
                               \nReward: {: .2f} +- {: .2f}  (Min: {: .2f} Max: {: .2f}) | Success Rate: {: .2f}\
                               \nReshaped: {: .2f} +- {: .2f}  (Min: {: .2f} Max: {: .2f}) | Bonus: {: .2f} +- {: .2f}  (Min: {: .2f} Max: {: .2f})\
                               \nFrames/Eps: {:.1f} +- {:.1f}  (Min: {}, Max {})\
                               \nEntropy: {: .3f} | Policy Loss: {: .3f} | Value Loss: {: .5f} | Loss: {: .3f} | Grad Norm: {: .3f}")

    total_start_time = time.time()
    while status['num_frames'] < args.num_steps:
        update_start_time = time.time()
        algo.number_updates = status['i']
        logs = algo.update_parameters()
        update_end_time = time.time()

        status['num_frames'] += logs["num_frames"]
        status['num_episodes'] += logs['episodes_done']
        status['i'] += 1

        total_ellapsed_time = int(time.time() - total_start_time)
        fps = logs["num_frames"] / (update_end_time - update_start_time)
        return_per_episode = utils.synthesize(logs["return_per_episode"])
        success_per_episode = utils.synthesize(
            [1 if r > 0 else 0 for r in logs["return_per_episode"]])
        reshaped_return_per_episode = utils.synthesize(logs["reshaped_return_per_episode"])
        reshaped_return_bonus_per_episode = utils.synthesize(logs["reshaped_return_bonus_per_episode"])
        num_frames_per_episode = utils.synthesize(logs["num_frames_per_episode"])

        data = [status['i'], status['num_episodes'], status['num_frames'],
                fps, total_ellapsed_time, 
                *return_per_episode.values(),
                success_per_episode['mean'],
                *reshaped_return_per_episode.values(),
                *reshaped_return_bonus_per_episode.values(),
                *num_frames_per_episode.values(),
                logs["entropy"], logs["policy_loss"], logs["value_loss"],
                logs["loss"], logs["grad_norm"]]

        logger.info(Fore.YELLOW + format_str.format(*data) + Fore.RESET)
        csv_writer.writerow(data)

        with open(status_path, 'w') as dst:
            json.dump(status, dst)


# This will be overriden by lamorel's launcher if used
@hydra.main(config_path='/home/shubham_mt22124/RL_Project/Grounding_LLMs_with_online_RL/experiments/configs', config_name='local_gpu_config.yaml')
def main(config_args):
    # lm server
    if config_args.lamorel_args.distributed_setup_args.n_llm_processes > 0:
        custom_lamorel_module_functions = {
            'value': ValueHeadModuleFn(config_args.lamorel_args.llm_args.model_type)
        }
        if config_args.rl_script_args.use_action_heads:
            custom_lamorel_module_functions['policy_head'] = ActionHeadsModuleFn(
                config_args.lamorel_args.llm_args.model_type,
                len(config_args.rl_script_args.action_space)
            )
            lamorel_scoring_module_key = "policy_head"
        else:
            custom_lamorel_module_functions['score'] = LogScoringModuleFn(
                config_args.lamorel_args.llm_args.model_type
            )
            lamorel_scoring_module_key = "score"

        lamorel_init()
        lm_server = Caller(config_args.lamorel_args, custom_updater=PPOUpdater(),
                           custom_module_functions=custom_lamorel_module_functions)

    # Env
    name_env = config_args.rl_script_args.name_environment
    seed = config_args.rl_script_args.seed
    envs = []
    subgoals = []
    number_envs = config_args.rl_script_args.number_envs
    list_actions = []
    for a in config_args.rl_script_args.action_space:
        list_actions.append(a.replace("_", " "))
    for i in range(number_envs):
        env = gym.make(name_env)
        env.seed(100 * seed + i)
        envs.append(env)
        subgoals.append(list_actions)

    envs = ParallelEnv(envs)

    if config_args.rl_script_args.reward_shaping_beta == 0:
        reshape_reward = reward_function
    else:
        reshape_reward = reward_function_shapped  # TODO ad the beta

    id_expe = config_args.rl_script_args.name_experiment + \
              '_nbr_env_{}_'.format(config_args.rl_script_args.number_envs) + \
              '{}_'.format(config_args.rl_script_args.name_model) + \
              'pretrained_{}_'.format(config_args.lamorel_args.llm_args.pretrained)

    if not config_args.lamorel_args.llm_args.pretrained:
        id_expe += 'load_embedding_{}_'.format(config_args.rl_script_args.load_embedding)

    if config_args.rl_script_args.use_action_heads:
        id_expe += 'use_action_heads_{}_'.format(config_args.rl_script_args.use_action_heads)

    if config_args.rl_script_args.nbr_obs != 3:
        id_expe += 'nbr_obs_{}_'.format(config_args.rl_script_args.nbr_obs)

    id_expe += 'nbr_actions_{}_'.format(len(config_args.rl_script_args.action_space))

    for a in config_args.rl_script_args.action_space:
        id_expe += a + '_'

    id_expe += 'shape_reward_beta_{}_'.format(config_args.rl_script_args.reward_shaping_beta) + \
               'seed_{}'.format(config_args.rl_script_args.seed)

    model_path = os.path.join(config_args.rl_script_args.saving_path_model, id_expe)
    if not os.path.exists(model_path):
        os.makedirs(model_path)
    if config_args.lamorel_args.distributed_setup_args.n_llm_processes > 0:
        if os.path.exists(config_args.rl_script_args.saving_path_model + "/" + id_expe + "/last/model.checkpoint"):
            # if model.checkpoint already exists that means update =! 0 and we reload the weights of the fine-tuned model
            lm_server.update([None for _ in range(config_args.lamorel_args.distributed_setup_args.n_llm_processes)],
                             [[None] for _ in range(config_args.lamorel_args.distributed_setup_args.n_llm_processes)],
                             id_expe=id_expe, load_fine_tuned_version=True,
                             saving_path_model=config_args.rl_script_args.saving_path_model)

        else:
            # in the case the model is not pretrained if necessary loads embedding
            os.makedirs(os.path.join(model_path, 'last'))
            os.makedirs(os.path.join(model_path, 'backup'))
            if not config_args.lamorel_args.llm_args.pretrained and config_args.rl_script_args.load_embedding:
                lm_server.update([None for _ in range(config_args.lamorel_args.distributed_setup_args.n_llm_processes)],
                                 [[None] for _ in range(config_args.lamorel_args.distributed_setup_args.n_llm_processes)],
                                 load_embedding=True, id_expe=id_expe,
                                 llm_path=config_args.lamorel_args.llm_args.model_path,
                                 saving_path_model=config_args.rl_script_args.saving_path_model,
                                 lr=config_args.rl_script_args.lr,
                                 beta1=config_args.rl_script_args.beta1,
                                 beta2=config_args.rl_script_args.beta2,
                                 adam_eps=config_args.rl_script_args.adam_eps)
            else:
                # save a first version of the llm that will after the first update become the first backup
                lm_server.update([None for _ in range(config_args.lamorel_args.distributed_setup_args.n_llm_processes)],
                                 [[None] for _ in range(config_args.lamorel_args.distributed_setup_args.n_llm_processes)],
                                 save_first_last=True, id_expe=id_expe,
                                 saving_path_model=config_args.rl_script_args.saving_path_model,
                                 lr=config_args.rl_script_args.lr,
                                 beta1=config_args.rl_script_args.beta1,
                                 beta2=config_args.rl_script_args.beta2,
                                 adam_eps=config_args.rl_script_args.adam_eps)

        algo = LLMPPOAgent(envs, lm_server, lamorel_scoring_module_key,
                           config_args.lamorel_args.distributed_setup_args.n_llm_processes,
                           config_args.rl_script_args.frames_per_proc,
                           config_args.rl_script_args.discount, config_args.rl_script_args.lr,
                           config_args.rl_script_args.beta1, config_args.rl_script_args.beta2,
                           config_args.rl_script_args.gae_lambda, config_args.rl_script_args.entropy_coef,
                           config_args.rl_script_args.value_loss_coef, config_args.rl_script_args.max_grad_norm,
                           config_args.rl_script_args.adam_eps, config_args.rl_script_args.clip_eps,
                           config_args.rl_script_args.epochs, config_args.rl_script_args.batch_size,
                           reshape_reward,
                           config_args.rl_script_args.name_experiment,
                           config_args.rl_script_args.saving_path_model,
                           config_args.rl_script_args.saving_path_logs, number_envs, subgoals,
                           config_args.rl_script_args.nbr_obs, id_expe,
                           config_args.rl_script_args.template_test)
    else:
        algo = DRRNAgent(envs, subgoals, reshape_reward, config_args.rl_script_args.spm_path, max_steps=number_envs * 4,
                         saving_path=config_args.rl_script_args.saving_path_model + "/" + id_expe, save_frequency=1)
    run_agent(config_args.rl_script_args, algo, id_expe)
    if config_args.lamorel_args.distributed_setup_args.n_llm_processes > 0:
        lm_server.close()


if __name__ == '__main__':
    main()
