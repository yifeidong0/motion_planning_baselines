import einops
import torch

from mp_baselines.planners.base import MPPlanner
from torch_robotics.torch_utils.torch_timer import Timer
from torch_robotics.torch_utils.torch_utils import tensor_linspace
from torch_robotics.trajectory.utils import smoothen_trajectory


class HybridPlanner(MPPlanner):
    """
    Runs a sampled-based planner to get an initial trajectory in position, followed by an optimization-based planner
    to optimize for positions and velocities.
    """

    def __init__(
            self,
            sample_based_planner,
            opt_based_planner,
            **kwargs
    ):
        super().__init__(
            "HybridSampleAndOptimizationPlanner",
            **kwargs
        )

        self.sample_based_planner = sample_based_planner
        self.opt_based_planner = opt_based_planner

    def render(self, ax, **kwargs):
        raise NotImplementedError

    def optimize(self, debug=False, return_iterations=False, **kwargs):
        with Timer() as t_hybrid:
            #################################################
            # Get initial position solution with a sample-based planner
            # Optimize
            traj_l = []
            with Timer() as t_sample_based:
                for _ in range(self.opt_based_planner.num_particles_per_goal):
                    with Timer() as t_sample_based_instance:
                        traj = self.sample_based_planner.optimize(refill_samples_buffer=True, debug=debug, **kwargs)
                    if debug:
                        print(f'Sample-based Planner Instance -- Optimization time: {t_sample_based_instance.elapsed:.3f} sec')
                    # If no solution was found, create a linear interpolated trajectory between start and finish, even
                    # if is not collision-free
                    if traj is None:
                        traj = tensor_linspace(
                            self.sample_based_planner.start_state, self.sample_based_planner.goal_state,
                            steps=self.opt_based_planner.traj_len
                        ).T
                    traj_l.append(traj)
            if debug:
                print(f'Sample-based Planner -- Optimization time: {t_sample_based.elapsed:.3f} sec')

            #################################################
            # Interpolate initial trajectory to desired trajectory length, smooth and set average velocity
            traj_pos_vel_l = []
            for traj in traj_l:
                traj_pos, traj_vel = smoothen_trajectory(
                    traj, traj_len=self.opt_based_planner.traj_len, dt=self.opt_based_planner.dt,
                    set_average_velocity=True, tensor_args=self.tensor_args
                )
                # Reshape for gpmp/sgpmp interface
                initial_traj_pos_vel = torch.cat((traj_pos, traj_vel), dim=-1)

                traj_pos_vel_l.append(initial_traj_pos_vel)

            initial_traj_pos_vel = torch.stack(traj_pos_vel_l)
            initial_traj_pos_vel = einops.rearrange(initial_traj_pos_vel, 'n h d -> 1 n h d')

            #################################################
            # Fine tune with an optimization-based planner

            # set initial position and velocity trajectory
            self.opt_based_planner.reset(initial_particle_means=initial_traj_pos_vel)

            # Optimize
            trajs_0 = self.opt_based_planner.get_traj()
            trajs_iters = torch.empty((self.opt_based_planner.opt_iters + 1, *trajs_0.shape))
            trajs_iters[0] = trajs_0
            with Timer() as t_opt_based:
                for i in range(self.opt_based_planner.opt_iters):
                    trajs = self.opt_based_planner.optimize(opt_iters=1, debug=debug, **kwargs)
                    trajs_iters[i + 1] = trajs
            if debug:
                print(f'Optimization-based Planner -- Optimization time: {t_opt_based.elapsed:.3f} sec')

        if debug:
            print(f'Hybrid-based Planner -- Optimization time: {t_hybrid.elapsed:.3f} sec')

        if return_iterations:
            return trajs_iters
        else:
            return trajs_iters[-1]
