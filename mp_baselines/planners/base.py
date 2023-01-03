from abc import ABC, abstractmethod
from typing import Tuple
import torch
from stoch_gpmp.costs.factors.mp_priors_multi import MultiMPPrior
from stoch_gpmp.costs.factors.unary_factor import UnaryFactor
from stoch_gpmp.costs.factors.gp_factor import GPFactor


class MPPlanner(ABC):
    """Base class for all planners."""

    def __init__(self, name: str, tensor_args: dict = None, **kwargs):
        self.name = name
        if tensor_args is None:
            tensor_args = {
                'device': torch.device('cpu'),
                'dtype': torch.float32,
            }
        self.tensor_args = tensor_args
        self._kwargs = kwargs

    @abstractmethod
    def optimize(self, opt_iters: int = 1, **observation) -> Tuple[bool, torch.Tensor]:
        """Plan a path from start to goal.

        Args:
            opt_iters: Number of optimization iters.
            observation: dict of observations.

        Returns:
            success: True if a path was found.
            path: Path from start to goal.
        """
        pass

    def __call__(self, opt_iters: int = 1, **observation) -> Tuple[bool, torch.Tensor]:
        """Plan a path from start to goal.

        Args:
            start: Start position.
            goal: Goal position.

        Returns:
            success: True if a path was found.
            path: Path from start to goal.
        """
        return self.optimize(opt_iters, **observation)

    def __repr__(self):
        return f"{self.name}({self._kwargs})"


class OptimizationPlanner(MPPlanner):

    def __init__(self, name: str,
                n_dofs: int,
                traj_len: int,
                num_particles_per_goal: int,
                n_iters: int,
                dt: float,
                start_state: torch.Tensor,
                cost=None,
                initial_particle_means=None,
                multi_goal_states: torch.Tensor = None,
                sigma_start_init: float = 0.001,
                sigma_goal_init: float = 0.001,
                sigma_gp_init: float = 10.,
                pos_only: bool = True,
                tensor_args: dict = None, **kwargs):
        super().__init__(name, tensor_args, **kwargs)
        self.n_dofs = n_dofs
        self.traj_len = traj_len
        self.num_particles_per_goal = num_particles_per_goal
        self.n_iters = n_iters
        self.dt = dt
        self.pos_only = pos_only

        self.start_state = start_state
        self.multi_goal_states = multi_goal_states
        if multi_goal_states is None:  # NOTE(an): if there is no goal, we assume xere is at least one solution
            self.num_goals = 1
        else:
            assert multi_goal_states.ndim == 2
            self.num_goals = multi_goal_states.shape[0]
        self.num_particles = self.num_goals * self.num_particles_per_goal
        self.cost = cost
        self.initial_particle_means = initial_particle_means
        if self.pos_only:
            self.d_state_opt = self.n_dofs
            self.start_state = self.start_state
        else:
            self.d_state_opt = 2 * self.n_dofs
            self.start_state = torch.cat([self.start_state, torch.zeros_like(self.start_state)], dim=-1)
            self.multi_goal_states = torch.cat([self.multi_goal_states, torch.zeros_like(self.multi_goal_states)], dim=-1)

        self.sigma_start_init = sigma_start_init
        self.sigma_goal_init = sigma_goal_init
        self.sigma_gp_init = sigma_gp_init

    def get_GP_prior(
            self,
            start_K,
            gp_K,
            goal_K,
            state_init,
            particle_means=None,
            goal_states=None,
    ):

        return MultiMPPrior(
            self.traj_len - 1,
            self.dt,
            2 * self.n_dofs,
            self.n_dofs,
            start_K,
            gp_K,
            state_init,
            K_g_inv=goal_K,  # NOTE(sasha) Assume same goal Cov. for now
            means=particle_means,
            goal_states=goal_states,
            tensor_args=self.tensor_args,
        )

    def get_random_trajs(self):
        # set zero velocity for GP prior
        if self.pos_only:
            start_state = torch.cat((self.start_state, torch.zeros_like(self.start_state)), dim=-1)
            if self.multi_goal_states is not None:
                multi_goal_states = torch.cat((self.multi_goal_states, torch.zeros_like(self.multi_goal_states)), dim=-1)
            else:
                multi_goal_states = None
        else:
            start_state = self.start_state
            multi_goal_states = self.multi_goal_states
        #========= Initialization factors ===============
        self.start_prior_init = UnaryFactor(
            self.n_dofs * 2,
            self.sigma_start_init,
            start_state,
            self.tensor_args,
        )

        self.gp_prior_init = GPFactor(
            self.n_dofs,
            self.sigma_gp_init,
            self.dt,
            self.traj_len - 1,
            self.tensor_args,
        )

        self.multi_goal_prior_init = []
        if multi_goal_states is not None:
            for i in range(self.num_goals):
                self.multi_goal_prior_init.append(
                    UnaryFactor(
                        self.n_dofs * 2,
                        self.sigma_goal_init,
                        multi_goal_states[i],
                        self.tensor_args,
                    )
                )
        self._traj_dist = self.get_GP_prior(
                self.start_prior_init.K,
                self.gp_prior_init.Q_inv[0],
                self.multi_goal_prior_init[0].K if multi_goal_states is not None else None,
                start_state,
                goal_states=multi_goal_states,
            )
        particles = self._traj_dist.sample(self.num_particles_per_goal).to(**self.tensor_args)
        if self.pos_only:
            particles = particles[..., :self.n_dofs]
        self.traj_dim = particles.shape
        del self._traj_dist  # free memory
        return particles.flatten(0, 1)

    def _get_traj(self):
        """
            Get position-velocity trajectory from control distribution.
        """
        trajs = self._particle_means.clone()
        if self.pos_only:
            # Linear velocity by finite differencing
            vels = (trajs[..., :-1, :] - trajs[..., 1:, :]) / self.dt
            # Pad end with zero-vel for planning
            vels = torch.cat(
                (vels, torch.zeros_like(vels[..., -1:, :])),
                dim=0,
            )
            trajs = torch.cat((trajs, vels), dim=1)
        return trajs

    def _get_costs(self, state_trajectories, **observation):
        if self.cost is None:
            costs = torch.zeros(self.num_particles, )
        else:
            costs = self.cost.eval(state_trajectories, **observation)
        return costs
