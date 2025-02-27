import torch
import torch.nn as nn
import numpy as np
import itertools
from tqdm import tqdm, trange

class ValueNetwork(nn.Module):
    def __init__(self, n, hidden_1_k=4, hidden_2_k=8):
        super().__init__()
        self.n = n
        self.linear_1 = nn.Linear(n ** 3, hidden_1_k * n ** 2)
        self.linear_2 = nn.Linear(hidden_1_k * n ** 2, hidden_2_k * n)
        self.linear_3 = nn.Linear(hidden_2_k * n, 1)

    def forward(self, costs):
        features = costs.flatten(start_dim=1) # preserve batch dimension
        hidden_1 = nn.functional.relu(self.linear_1(features))
        hidden_2 = nn.functional.relu(self.linear_2(hidden_1))
        out = self.linear_3(hidden_2)
        return out[:, 0] 

class ClassicalValueEstimator:
    def __init__(self, n):
        self.n = n

    def __call__(self, costs):
        if self.n == 0:
            return torch.zeros_like(costs)[:, 0, 0, 0]
        if self.n == 1:
            return costs[:, 0, 0, 0]

class UniformGenerator:
    def get_batch(self, batch_size, problem_size):
        return torch.rand((batch_size, problem_size, problem_size, problem_size))
    def get_instance(self, problem_size):
        return torch.rand((problem_size, problem_size, problem_size))

class Agent:

    generator = UniformGenerator()
    value_network_factory = ValueNetwork
    
    pretrain_lr = 3e-4
    num_pretrain_iters = 300
    batch_size = 64
    
    finetune_lr = 1e-4
    num_finetune_iters = 20
    finetune_batch_size = 50
    
    def __init__(self,
        n=2,
        generator=None,
        value_network_factory=None,
        logs_folder="./logs",
        weights_folder="./weights",
        hyper_params=None):
        if value_network_factory is not None:
            self.value_network_factory = value_network_factory
        if generator is not None:
            self.generator = generator
        self.logs_folder = logs_folder
        self.weights_folder = weights_folder
        self.n = 2 # Will be later updated to match user requested n
        if hyper_params is not None:
            self.__dict__.update(hyper_params)

        self.value_networks = [ClassicalValueEstimator(n=0), ClassicalValueEstimator(n=1)]
        for i in range(2, n):
            self.pretrain_new_network()
        if n > 2:
            self.fine_tune()
    
    def log(self, name, data):
        if self.logs_folder != None:
              np.save(open(f"{self.logs_folder}/{name}.npy", "wb"), np.array(data))
    
    def save_weights(self, name, nn_module):
        if self.weights_folder != None:
            torch.save(nn_module.state_dict(), f"{self.weights_folder}/{name}.pt")
    
    @classmethod
    def load(cls, n, save_prefix="finetune"):
        agent = cls()
        for i in range(2, n):
            agent.value_networks.append(self.value_network_factory(n=i))
            agent.value_networks[-1].load_state_dict(torch.load(f"{self.weights_folder}/{save_prefix}-{i}.pt"))
            agent.n += 1
        return agent
    
    def pretrain_new_network(self):
        new_network = self.value_network_factory(n=self.n)
        criterion = nn.MSELoss()
        optimizer = torch.optim.Adam(new_network.parameters(), lr=self.pretrain_lr)
        loss_history = []
        iter_multiplier = (4 if self.n == 2 else 1)
        for iteration in trange(self.num_pretrain_iters * iter_multiplier,
                                    desc=f"Pretraining for size {self.n:2d}"):
            optimizer.zero_grad()
            costs = self.generator.get_batch(self.batch_size, self.n)
            pred_values = new_network(costs)
            best_values = torch.tensor([self.evaluate_position(cost) for cost in costs])
            loss = criterion(pred_values, best_values)
            loss.backward()
            optimizer.step()
            loss_history.append(loss.item())
        self.log(f"loss_{self.n}", loss_history)
        self.save_weights(f"pretrain-{self.n}", new_network)
        self.value_networks.append(new_network)
        self.n += 1
    
    def act(self, cost):
        return sum(self.get_rewards(cost))

    def get_rewards(self, cost, return_positions=False):
        rewards = []
        positions = []
        while cost.shape[0] > 1:
            _, best_action = self.evaluate_position(cost, return_best_move=True)
            j, k = best_action
            j_left = [i for i in range(cost.shape[0]) if i != j]
            k_left = [i for i in range(cost.shape[0]) if i != k]
            rewards.append(cost[0, j, k])
            if return_positions:
                positions.append(cost)
            cost = cost[1:][:, j_left][:, :, k_left] # can't find a better way
        rewards.append(cost.item())
        positions.append(cost)
        if return_positions:
            return rewards, positions
        return rewards

    @torch.no_grad()
    def evaluate_position(self, cost, return_best_move=False):
        size = cost.shape[0]
        if size == 1:
            return cost.item(), (0, 0)
        best_pos, best_reward = None, -1
        for j, k in itertools.product(range(size), repeat=2):
            j_left = [i for i in range(size) if i != j]
            k_left = [i for i in range(size) if i != k]
            cost_left = cost[1:][:, j_left][:, :, k_left] # can't find a better way
            reward = cost[0, j, k].item() + self.value_networks[size-1](cost_left.unsqueeze(dim=0)).item()
            if reward > best_reward:
                best_reward = reward
                best_pos = (j, k)
        if return_best_move:
            return best_reward, best_pos
        return best_reward

    def fine_tune(self):
        loss_history = []
        optimizer = torch.optim.Adam(
            itertools.chain(*[net.parameters() for net in self.value_networks[2:]]),
            lr=self.finetune_lr)
        criterion = nn.MSELoss()
        for iteration in trange(self.num_finetune_iters, desc=f"Fine-tuning"):
            optimizer.zero_grad()
            all_rewards = []
            all_pred_rewards = []
            for _ in range(self.finetune_batch_size):
                cost = self.generator.get_instance(self.n - 1)
                rewards, positions = self.get_rewards(cost, return_positions=True)
                for i in range(self.n - 2, 0, -1):
                    rewards[i - 1] += rewards[i] # total reward is trailing sum of the immediate ones
                pred_rewards = torch.concat(
                    [self.value_networks[self.n - i - 1](pos.unsqueeze(dim=0)) for i, pos in enumerate(positions)])
                all_rewards.append(torch.tensor(rewards))
                all_pred_rewards.append(pred_rewards)
            loss = criterion(torch.concat(all_pred_rewards), torch.concat(all_rewards))
            loss.backward()
            optimizer.step()
            loss_history.append(loss.item())
        self.log("loss_finetune", loss_history)
        for i in range(2, self.n):
            self.save_weights(f"finetune-{i}", self.value_networks[i])
