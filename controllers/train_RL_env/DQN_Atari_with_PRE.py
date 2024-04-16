# import gym, 
import random, pickle, os.path, math, glob

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
# %matplotlib inline

import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import pdb
import random

# from gym.wrappers import AtariPreprocessing, LazyFrames, FrameStack

from IPython.display import clear_output
from torch.utils.tensorboard import SummaryWriter


#------------------------------------------------------------------
# random seed 
def setup_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.deterministic = True

setup_seed(525)
#------------------------------------------------------------------

# USE_CUDA = torch.cuda.is_available()
# EPS_START = 0.9
# EPS_END = 0.05
# EPS_DECAY = int(5e5)
# TAU = 0.005
# BATCH_SIZE = 128 # 512 
# LR = 2e-4
# MAX_BUFF = 100000

# print(USE_CUDA)
dtype = torch.cuda.FloatTensor if torch.cuda.is_available() else torch.FloatTensor
Variable = lambda *args, **kwargs: autograd.Variable(*args, **kwargs).cuda() if USE_CUDA else autograd.Variable(*args, **kwargs)


# SumTree
# a binary tree data structure where the parent’s value is the sum of its children
class SumTree:
    write = 0

    def __init__(self, capacity):
        self.capacity = capacity
        self.tree = np.zeros(2 * capacity - 1)
        self.data = np.zeros(capacity, dtype=object)
        self.n_entries = 0

    # update to the root node
    def _propagate(self, idx, change):
        parent = (idx - 1) // 2

        self.tree[parent] += change

        if parent != 0:
            self._propagate(parent, change)

    # find sample on leaf node
    def _retrieve(self, idx, s):
        left = 2 * idx + 1
        right = left + 1

        if left >= len(self.tree):
            return idx

        if s <= self.tree[left]:
            return self._retrieve(left, s)
        else:
            return self._retrieve(right, s - self.tree[left])

    def total(self):
        return self.tree[0]

    # store priority and sample
    def add(self, p, data):
        idx = self.write + self.capacity - 1

        self.data[self.write] = data
        self.update(idx, p)

        self.write += 1
        if self.write >= self.capacity:
            self.write = 0

        if self.n_entries < self.capacity:
            self.n_entries += 1

    # update priority
    def update(self, idx, p):
        change = p - self.tree[idx]

        self.tree[idx] = p
        self._propagate(idx, change)

    # get priority and sample
    def get(self, s):
        idx = self._retrieve(0, s)
        dataIdx = idx - self.capacity + 1

        return (idx, self.tree[idx], self.data[dataIdx])

class DQN(nn.Module):
    def __init__(self, in_channels=4, num_actions=5):
        """
        Initialize a deep Q-learning network as described in
        https://storage.googleapis.com/deepmind-data/assets/papers/DeepMindNature14236Paper.pdf
        Arguments:
            in_channels: number of channel of input.
                i.e The number of most recent frames stacked together as describe in the paper
            num_actions: number of action-value to output, one-to-one correspondence to action in game.
        """
        super(DQN, self).__init__()
        self.conv1 = nn.Conv2d(in_channels, 32, kernel_size=8, stride=4)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=4, stride=2)
        self.conv3 = nn.Conv2d(64, 64, kernel_size=3, stride=1)
        self.fc4 = nn.Linear(7 * 7 * 64, 512)
        self.fc5 = nn.Linear(512, num_actions)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = F.relu(self.conv3(x))
        x = F.relu(self.fc4(x.reshape(x.size(0), -1)))
        return self.fc5(x)

class Memory_Buffer_PER(object):
    # stored as ( s, a, r, s_ ) in SumTree
    def __init__(self, memory_size=1000, a = 0.6, e = 0.01):
        self.tree =  SumTree(memory_size)
        self.memory_size = memory_size
        self.prio_max = 0.1
        self.a = a
        self.e = e
        
    def push(self, state, action, reward, next_state, done):
        data = (state, action, reward, next_state, done)
        p = (np.abs(self.prio_max) + self.e) ** self.a #  proportional priority
        self.tree.add(p, data)

    def sample(self, batch_size):
        states, actions, rewards, next_states, dones = [], [], [], [], []
        idxs = []
        segment = self.tree.total() / batch_size
        priorities = []
        for i in range(batch_size):
            a = segment * i
            b = segment * (i + 1)
            s = random.uniform(a, b)
            idx, p, data = self.tree.get(s)
            
            state, action, reward, next_state, done= data
            states.append(state)
            actions.append(action)
            rewards.append(reward)
            next_states.append(next_state)
            dones.append(done)
            priorities.append(p)
            idxs.append(idx)
        return idxs, np.concatenate(states), actions, rewards, np.concatenate(next_states), dones
    
    def update(self, idxs, errors):
        self.prio_max = max(self.prio_max, max(np.abs(errors)))
        for i, idx in enumerate(idxs):
            p = (np.abs(errors[i]) + self.e) ** self.a
            self.tree.update(idx, p) 
        
    def size(self):
        return self.tree.n_entries

class DQNAgent:
    def __init__(self, 
                 env, 
                 epsilon = 1,
                 EPS_START = 0.9,
                 EPS_END = 0.05,
                 EPS_DECAY = int(5e5),
                 TAU = 0.005,
                 BATCH_SIZE = 128,
                 LR = 2e-4,
                 MAX_BUFF = 100000,
                 LEARNING_START = 10000):
        # ----------------------------
        # random seed 
        random.seed(env.seed) 
        torch.manual_seed(env.seed)
        # ----------------------------
        # set hyperparameter
        self.EPS_START = EPS_START
        self.EPS_END = EPS_END
        self.EPS_DECAY = EPS_DECAY
        self.TAU = TAU
        self.BATCH_SIZE = BATCH_SIZE
        self.LR = LR
        self.MAX_BUFF = MAX_BUFF
        self.LEARNING_START = LEARNING_START        
        # ----------------------------
        
        state_channel = env.observation_space.shape[0]
        
        self.epsilon = epsilon
        self.steps_done = 0
        self.action_space = env.action_space
        self.memory_buffer = Memory_Buffer_PER(self.MAX_BUFF)
        self.DQN = DQN(in_channels = state_channel, num_actions = self.action_space.n)
        self.DQN_target = DQN(in_channels = state_channel, num_actions = self.action_space.n)
        self.DQN_target.load_state_dict(self.DQN.state_dict())

        self.USE_CUDA = torch.cuda.is_available()
        if self.USE_CUDA:
            self.DQN = self.DQN.cuda()
            self.DQN_target = self.DQN_target.cuda()
                   
        self.optimizer = optim.AdamW(self.DQN.parameters(), lr=self.LR, amsgrad=True)

    def observe(self, state):
        # from Lazy frame to tensor
        # state = torch.from_numpy(lazyframe.__array__()[None]/255).float()
        state = torch.from_numpy(state/255).float() if isinstance(state, np.ndarray) else state
        state = state.unsqueeze(0).permute(0, 3, 1, 2)
        if self.USE_CUDA:
            state = state.cuda()
        return state

    def value(self, state):
        q_values = self.DQN(state)
        return q_values

    def act(self, state, epsilon = None):
        """
        sample actions with epsilon-greedy policy
        recap: with p = epsilon pick random action, else pick action with highest Q(s,a)
        """
        if epsilon is None:
            self.epsilon = self.EPS_END + (self.EPS_START - self.EPS_END) * \
                math.exp(-1. * self.steps_done / self.EPS_DECAY)
        else:
            self.epsilon = epsilon


        q_values = self.value(state).cpu().detach().numpy()
        if random.random()<self.epsilon:
            aciton = random.randrange(self.action_space.n)
        else:
            aciton = q_values.argmax(1)[0]
        
        self.steps_done += 1
        return aciton
    
    def update_weights(self):
        target_net_state_dict = self.DQN_target.state_dict()
        policy_net_state_dict = self.DQN.state_dict()
        for key in policy_net_state_dict:
            target_net_state_dict[key] = policy_net_state_dict[key]*self.TAU + target_net_state_dict[key]*(1-self.TAU)
        self.DQN_target.load_state_dict(target_net_state_dict)

    def compute_td_loss(self,idxs, states, actions, rewards, next_states, is_done, gamma=0.99):
        """ Compute td loss using torch operations only. Use the formula above. """
        actions = torch.tensor(actions).long()    # shape: [batch_size]
        rewards = torch.tensor(rewards, dtype =torch.float)  # shape: [batch_size]
        is_done = torch.tensor(is_done).bool()  # shape: [batch_size]
        
        if self.USE_CUDA:
            actions = actions.cuda()
            rewards = rewards.cuda()
            is_done = is_done.cuda()

        # get q-values for all actions in current states
        predicted_qvalues = self.DQN(states)

        # select q-values for chosen actions
        predicted_qvalues_for_actions = predicted_qvalues[
          range(states.shape[0]), actions
        ]

        # compute q-values for all actions in next states
        predicted_next_qvalues = self.DQN_target(next_states) # YOUR CODE

        # compute V*(next_states) using predicted next q-values
        next_state_values =  predicted_next_qvalues.max(-1)[0] # YOUR CODE

        # compute "target q-values" for loss - it's what's inside square parentheses in the above formula.
        target_qvalues_for_actions = rewards + gamma *next_state_values # YOUR CODE

        # at the last state we shall use simplified formula: Q(s,a) = r(s,a) since s' doesn't exist
        target_qvalues_for_actions = torch.where(
            is_done, rewards, target_qvalues_for_actions)

        # mean squared error loss to minimize
        errors = (predicted_qvalues_for_actions - target_qvalues_for_actions).detach().cpu().squeeze().tolist()
        self.memory_buffer.update(idxs, errors)
        loss = F.smooth_l1_loss(predicted_qvalues_for_actions, target_qvalues_for_actions.detach())

        return loss

    # def sample_from_buffer(self, batch_size):
    #     states, actions, rewards, next_states, dones = [], [], [], [], []
    #     for i in range(batch_size):
    #         idx = random.randint(0, self.memory_buffer.size() - 1)
    #         data = self.memory_buffer.tree[idx]
    #         frame, action, reward, next_frame, done= data
    #         states.append(self.observe(frame))
    #         actions.append(action)
    #         rewards.append(reward)
    #         next_states.append(self.observe(next_frame))
    #         dones.append(done)
    #     return torch.cat(states), actions, rewards, torch.cat(next_states), dones
    def sample_from_buffer(self, batch_size):
        states, actions, rewards, next_states, dones = [], [], [], [], []
        idxs = []
        segment = self.memory_buffer.tree.total() / batch_size
        priorities = []

        for i in range(batch_size):
            a = segment * i
            b = segment * (i + 1)
            s = random.uniform(a, b)
            idx, p, data = self.memory_buffer.tree.get(s)
            
            frame, action, reward, next_frame, done= data
            states.append(self.observe(frame))
            actions.append(action)
            rewards.append(reward)
            next_states.append(self.observe(next_frame))
            dones.append(done)
            priorities.append(p)
            idxs.append(idx)
        return idxs, torch.cat(states), actions, rewards, torch.cat(next_states), dones

    def learn_from_experience(self):
        if self.memory_buffer.size() > self.BATCH_SIZE:
            idxs,states, actions, rewards, next_states, dones = self.sample_from_buffer(self.BATCH_SIZE)
            td_loss = self.compute_td_loss(idxs,states, actions, rewards, next_states, dones)
            self.optimizer.zero_grad()
            td_loss.backward()
            for param in self.DQN.parameters():
                param.grad.data.clamp_(-1, 1)

            self.optimizer.step()
            self.loss = td_loss.item()
            return(td_loss.item())
        else:
            self.loss = 0
            return(0)
    
    
    def save(self, log_dir):
        torch.save(self.DQN_target, f"{log_dir}/good_model.pt")
        torch.save(self.DQN_target.state_dict(), f"{log_dir}/good_model_state_dict.pt")
        
    def load(self, model_dir, is_test=True):
        self.DQN = torch.load(model_dir)
        
        if is_test:
            self.DQN.eval()


if __name__ == '__main__':

    # Training DQN in PongNoFrameskip-v4
    env = gym.make('PongNoFrameskip-v4')
    env = AtariPreprocessing(env,
                            scale_obs=False,
                            terminal_on_life_loss=True,
                            )
    env = FrameStack(env, num_stack=4)

    gamma = 0.99
    epsilon_max = 1
    epsilon_min = 0.05
    eps_decay = 30000
    frames = 2000000
    USE_CUDA = False
    learning_rate = 2e-4
    max_buff = 100000
    update_tar_interval = 1000
    batch_size = 32
    print_interval = 1000
    log_interval = 1000
    learning_start = 10000
    win_reward = 18     # Pong-v4
    win_break = True

    action_space = env.action_space
    action_dim = env.action_space.n
    state_dim = env.observation_space.shape[1]
    state_channel = env.observation_space.shape[0]
    agent = DQNAgent(in_channels = state_channel, 
                    action_space = action_space, 
                    USE_CUDA = USE_CUDA, 
                    lr = learning_rate,
                    memory_size = max_buff)

    frame, _ = env.reset()

    episode_reward = 0
    all_rewards = []
    losses = []
    episode_num = 0
    is_win = False
    # tensorboard
    summary_writer = SummaryWriter(log_dir = "DQN_stackframe", comment= "good_makeatari")

    # e-greedy decay
    epsilon_by_frame = lambda frame_idx: epsilon_min + (epsilon_max - epsilon_min) * math.exp(
                -1. * frame_idx / eps_decay)
    # plt.plot([epsilon_by_frame(i) for i in range(10000)])

    for i in range(frames):
        epsilon = epsilon_by_frame(i)
        state_tensor = agent.observe(frame)
        action = agent.act(state_tensor, epsilon)

        next_frame, reward, terminated, truncated, _ = env.step(action)
        done = terminated or truncated
        
        episode_reward += reward
        agent.memory_buffer.push(frame, action, reward, next_frame, done)
        frame = next_frame

        loss = 0
        if agent.memory_buffer.size() >= learning_start:
            loss = agent.learn_from_experience(batch_size)
            losses.append(loss)

        if i % print_interval == 0:
            print("frames: %5d, reward: %5f, loss: %4f, epsilon: %5f, episode: %4d" % (i, np.mean(all_rewards[-10:]), loss, epsilon, episode_num))
            summary_writer.add_scalar("Temporal Difference Loss", loss, i)
            summary_writer.add_scalar("Mean Reward", np.mean(all_rewards[-10:]), i)
            summary_writer.add_scalar("Epsilon", epsilon, i)

        if i % update_tar_interval == 0:
            agent.DQN_target.load_state_dict(agent.DQN.state_dict())

        if done:

            frame, _ = env.reset()

            all_rewards.append(episode_reward)
            episode_reward = 0
            episode_num += 1
            avg_reward = float(np.mean(all_rewards[-100:]))

    summary_writer.close()