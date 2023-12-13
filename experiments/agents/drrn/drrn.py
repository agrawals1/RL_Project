'''
This code has been taken from https://github.com/microsoft/tdqn and modified to match our needs
'''
import numpy as np
import logging

logger = logging.getLogger(__name__)
from tqdm import tqdm
from collections import deque

import torch
import torch.nn as nn
import torch.nn.functional as F
from .model import DRRN
from .utils.memory import PrioritizedReplayMemory, Transition, State
import sentencepiece as spm

import pickle

import babyai.rl
import os
import sys
current_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)
from base_agent import BaseAgent

# Accelerate
from accelerate import Accelerator

accelerator = Accelerator()
device = accelerator.state.device


class DRRNAgent(BaseAgent):
    def __init__(self, envs, subgoals, reshape_reward, spm_path, saving_path, gamma=0.9, batch_size=64, memory_size=5000000,
                 priority_fraction=0, clip=5, embedding_dim=128, hidden_dim=128, lr=0.0001, max_steps=64, save_frequency=10):
        super().__init__(envs)
        self.subgoals = subgoals
        self.reshape_reward = reshape_reward
        self.gamma = gamma
        self.batch_size = batch_size
        self.sp = spm.SentencePieceProcessor()
        self.sp.Load(spm_path)
        ## self.memory = ReplayMemory(memory_size)     ## PJ: Changing to more memory efficient memory, since the pickle files are enormous
        self.memory = PrioritizedReplayMemory(capacity=memory_size,
                                              priority_fraction=priority_fraction)  ## PJ: Changing to more memory efficient memory, since the pickle files are enormous
        self.clip = clip
        self.network = DRRN(len(self.sp), embedding_dim, hidden_dim).to(device)
        self.optimizer = torch.optim.Adam(self.network.parameters(), lr=lr)

        self.max_steps = max_steps

        # Stateful env
        self.obs, self.infos = self.env.reset()
        self.n_envs = len(self.obs)
        self.obs_queue = [deque([], maxlen=3) for _ in range(self.n_envs)]
        self.acts_queue = [deque([], maxlen=2) for _ in range(self.n_envs)]
        for j in range(self.n_envs):
            self.obs_queue[j].append(self.infos[j]['descriptions'])
        prompts = [self.generate_prompt(goal=self.obs[j]['mission'], subgoals=self.subgoals[j],
                                             deque_obs=self.obs_queue[j], deque_actions=self.acts_queue[j])
                   for j in range(self.n_envs)]
        self.states = self.build_state(prompts)
        self.encoded_actions = self.encode_actions(self.subgoals)
        self.logs = {
            "return_per_episode": [],
            "reshaped_return_per_episode": [],
            "reshaped_return_bonus_per_episode": [],
            "num_frames_per_episode": [],
            "num_frames": self.max_steps,
            "episodes_done": 0,
            "entropy": 0,
            "policy_loss": 0,
            "value_loss": 0,
            "grad_norm": 0,
            "loss": 0
        }
        self.returns = [0 for _ in range(self.n_envs)]
        self.reshaped_returns = [0 for _ in range(self.n_envs)]
        self.frames_per_episode = [0 for _ in range(self.n_envs)]

        self.save_frequency = save_frequency
        self.saving_path = saving_path
        self.__inner_counter = 0

    def observe(self, state, act, rew, next_state, next_acts, done):
        self.memory.push(False, state, act, rew, next_state, next_acts, done)

    def build_state(self, obs):
        return [State(self.sp.EncodeAsIds(o)) for o in obs]

    def encode_actions(self, acts):
        return [self.sp.EncodeAsIds(a) for a in acts]

    def act(self, states, poss_acts, sample=True):
        """ Returns a string action from poss_acts. """
        act_values = self.network.forward(states, poss_acts)
        if sample:
            act_probs = [F.softmax(vals, dim=0) for vals in act_values]
            act_idxs = [torch.multinomial(probs, num_samples=1).item()
                        for probs in act_probs]
        else:
            act_idxs = [vals.argmax(dim=0).item() for vals in act_values]

        act_ids = [poss_acts[batch][idx] for batch, idx in enumerate(act_idxs)]
        return act_ids, act_idxs, act_values

    def update(self):
        if len(self.memory) < self.batch_size:
            return

        transitions = self.memory.sample(self.batch_size)
        batch = Transition(*zip(*transitions))

        # Compute Q(s', a') for all a'
        next_qvals = self.network(batch.next_state, batch.next_acts)
        # Take the max over next q-values
        next_qvals = torch.tensor([vals.max() for vals in next_qvals], device=device)
        # Zero all the next_qvals that are done
        next_qvals = next_qvals * (1 - torch.tensor(batch.done, dtype=torch.float, device=device))
        targets = torch.tensor(batch.reward, dtype=torch.float, device=device) + self.gamma * next_qvals

        # Next compute Q(s, a)
        # Nest each action in a list - so that it becomes the only admissible cmd
        nested_acts = tuple([[a] for a in batch.act])
        qvals = self.network(batch.state, nested_acts)
        # Combine the qvals: Maybe just do a greedy max for generality
        qvals = torch.cat(qvals)

        # Compute Huber loss
        loss = F.smooth_l1_loss(qvals, targets.detach())
        self.optimizer.zero_grad()
        loss.backward()
        # loss.backward()
        nn.utils.clip_grad_norm_(self.network.parameters(), self.clip)
        self.optimizer.step()
        return loss

    def update_parameters(self):
        episodes_done = 0
        for i in tqdm(range(self.max_steps // self.n_envs), ascii=" " * 9 + ">", ncols=100):
            action_ids, action_idxs, _ = self.act(self.states, self.encoded_actions, sample=True)
            actions = [_subgoals[idx] for _subgoals, idx in zip(self.subgoals, action_idxs)]
            if len(self.subgoals[0]) > 6:
                # only useful when we test the impact of the number of actions
                real_a = np.copy(action_idxs)
                real_a[real_a > 6] = 6
                obs, rewards, dones, infos = self.env.step(real_a)
            else:
                obs, rewards, dones, infos = self.env.step(action_idxs)
            reshaped_rewards = [self.reshape_reward(reward=r)[0] for r in rewards]
            for j in range(self.n_envs):
                self.returns[j] += rewards[j]
                self.reshaped_returns[j] += reshaped_rewards[j]
                self.frames_per_episode[j] += 1
                if dones[j]:
                    episodes_done += 1
                    self.logs["num_frames_per_episode"].append(self.frames_per_episode[j])
                    self.frames_per_episode[j] = 0
                    self.logs["return_per_episode"].append(self.returns[j])
                    self.returns[j] = 0
                    self.logs["reshaped_return_per_episode"].append(self.reshaped_returns[j])
                    self.logs["reshaped_return_bonus_per_episode"].append(self.reshaped_returns[j])
                    self.reshaped_returns[j] = 0
                    # reinitialise memory of past observations and actions
                    self.obs_queue[j].clear()
                    self.acts_queue[j].clear()
                else:
                    self.acts_queue[j].append(actions[j])
                    self.obs_queue[j].append(infos[j]['descriptions'])

            next_prompts = [self.generate_prompt(goal=obs[j]['mission'], subgoals=self.subgoals[j],
                                                 deque_obs=self.obs_queue[j],
                                                 deque_actions=self.acts_queue[j])
                            for j in range(self.n_envs)]
            next_states = self.build_state(next_prompts)
            for state, act, rew, next_state, next_poss_acts, done in \
                    zip(self.states, action_ids, reshaped_rewards, next_states, self.encoded_actions, dones):
                self.observe(state, act, rew, next_state, next_poss_acts, done)
            self.states = next_states
            # self.logs["num_frames"] += self.n_envs

        loss = self.update()
        self.__inner_counter += 1
        if self.__inner_counter % self.save_frequency == 0:
            self.save()

        if loss is not None:
            self.logs["loss"] = loss.detach().cpu().item()

        logs = {}
        for k, v in self.logs.items():
            if isinstance(v, list):
                logs[k] = v[:-episodes_done]
            else:
                logs[k] = v
        logs["episodes_done"] = episodes_done
        return logs

    def generate_trajectories(self, dict_modifier, n_tests, language='english'):
        if language == "english":
            generate_prompt = self.generate_prompt
            subgoals = self.subgoals
        elif language == "french":
            generate_prompt = self.generate_prompt_french
            subgoals = [[self.prompt_modifier(sg, self.dict_translation_actions) for sg in sgs]
                        for sgs in self.subgoals]

        episodes_done = 0
        pbar = tqdm(range(n_tests), ascii=" " * 9 + ">", ncols=100)
        while episodes_done < n_tests:
            # Do one agent-environment interaction
            prompts = [
                self.prompt_modifier(
                    generate_prompt(goal=self.obs[j]['mission'],
                                    subgoals=subgoals[j],
                                    deque_obs=self.obs_queue[j],
                                    deque_actions=self.acts_queue[j]),
                    dict_modifier)
                for j in range(self.n_envs)]
            self.states = self.build_state(prompts)
            action_ids, action_idxs, _ = self.act(self.states, self.encoded_actions, sample=True)
            actions = [_subgoals[idx] for _subgoals, idx in zip(self.subgoals, action_idxs)]

            if len(self.subgoals[0]) > 6:
                # only useful when we test the impact of the number of actions
                real_a = np.copy(action_idxs)
                real_a[real_a > 6] = 6
                obs, rewards, dones, infos = self.env.step(real_a)
            else:
                obs, rewards, dones, infos = self.env.step(action_idxs)
            reshaped_rewards = [self.reshape_reward(reward=r)[0] for r in rewards]

            for j in range(self.n_envs):
                self.returns[j] += rewards[j]
                self.reshaped_returns[j] += reshaped_rewards[j]
                self.frames_per_episode[j] += 1
                if dones[j]:
                    episodes_done += 1
                    pbar.update(1)
                    self.logs["num_frames_per_episode"].append(self.frames_per_episode[j])
                    self.frames_per_episode[j] = 0
                    self.logs["return_per_episode"].append(self.returns[j])
                    self.returns[j] = 0
                    self.logs["reshaped_return_per_episode"].append(self.reshaped_returns[j])
                    self.logs["reshaped_return_bonus_per_episode"].append(self.reshaped_returns[j])
                    self.reshaped_returns[j] = 0
                    # reinitialise memory of past observations and actions
                    self.obs_queue[j].clear()
                    self.acts_queue[j].clear()
                else:
                    self.acts_queue[j].append(actions[j])
                    self.obs_queue[j].append(infos[j]['descriptions'])

            self.obs = obs
            next_prompts = [self.prompt_modifier(generate_prompt(goal=obs[j]['mission'], subgoals=subgoals[j],
                                                                 deque_obs=self.obs_queue[j],
                                                                 deque_actions=self.acts_queue[j]),
                                                 dict_modifier)
                            for j in range(self.n_envs)]
            next_states = self.build_state(next_prompts)

            self.states = next_states
            # self.logs["num_frames"] += self.n_envs
        pbar.close()

        logs = {}
        for k, v in self.logs.items():
            if isinstance(v, list):
                logs[k] = v[:]
            else:
                logs[k] = v
        logs["episodes_done"] = episodes_done
        return None, logs

    def load(self):
        try:
            with open(self.saving_path + "/memory.pkl", 'rb') as _file:
                saved_memory = pickle.load(_file)
            self.memory = saved_memory
            self.optimizer.load_state_dict(torch.load(self.saving_path + "/optimizer.checkpoint"))
        except Exception as err:
            print(f"Encountered the following exception when trying to load the memory, an empty memory will be used instead: {err}")

        self.network.load_state_dict(torch.load(self.saving_path + "/model.checkpoint"))


    def save(self):
        torch.save(self.network.state_dict(), self.saving_path + "/model.checkpoint")
        torch.save(self.optimizer.state_dict(), self.saving_path + "/optimizer.checkpoint")
        with open(self.saving_path + "/memory.pkl", 'wb') as _file:
            pickle.dump(self.memory, _file)