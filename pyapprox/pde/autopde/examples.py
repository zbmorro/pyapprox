#!/usr/bin/env python
import torch
import numpy as np
import matplotlib.pyplot as plt
from functools import partial

from pyapprox.pde.autopde.solvers import (
    Function, SteadyStatePDE, SteadyStateAdjointPDE
)
from pyapprox.pde.autopde.physics import (
    AdvectionDiffusionReaction
)
from pyapprox.pde.autopde.mesh import (
    CartesianProductCollocationMesh
)
from pyapprox.pde.karhunen_loeve_expansion import MeshKLE

from pyapprox.util.utilities import check_gradients


def fwd_solver_finite_difference_wrapper(
        solver, functional, set_params, params, **newton_kwargs):
    # Warning newton tol must be smaller than finite difference step size
    set_params(solver.residual, torch.as_tensor(params[:, 0]))
    fd_sol = solver.solve(**newton_kwargs)
    qoi = np.asarray([functional(fd_sol, torch.as_tensor(params[:, 0]))])
    return qoi


def loglike_functional(obs, obs_indices, noise_std, sol, params):
    assert obs.ndim == 1 and sol.ndim == 1
    nobs = obs_indices.shape[0]
    tmp = 1/(2*noise_std**2)
    ll = 0.5*np.log(tmp/np.pi)*nobs
    pred_obs = sol[obs_indices]
    ll += torch.sum(-(obs-pred_obs)**2*tmp)
    print(pred_obs.detach().numpy(), 'pobs')
    return ll


def zeros_fun_axis_1(x):
    # axis_1 used when x is mesh points
    return np.zeros((x.shape[1], 1))

def set_kle_diff_params(kle, residual, params):
    kle_vals = kle(params[:kle.nterms, None])
    print(kle_vals[:3, 0], 'kle')
    residual._diff_fun = partial(residual.mesh.interpolate, kle_vals)


def advection_diffusion():

    true_kle_params = torch.tensor([1.0, 1.0], dtype=torch.double)
    true_noise_std = 0.1
    true_params = true_kle_params
    obs_indices = np.array([200, 225, 300])
    length_scale = 0.1
    nrandom_vars = 2

    orders = [20, 20]
    domain_bounds = [0, 1, 0, 1]
    mesh = CartesianProductCollocationMesh(domain_bounds, orders)

    kle = MeshKLE(mesh.mesh_pts, use_log=True, use_torch=True)
    kle.compute_basis(length_scale, nterms=nrandom_vars)

    def vel_fun(xx):
        return torch.hstack((
            torch.ones(xx.shape[1], 1), torch.zeros(xx.shape[1], 1)))

    react_funs = [
        lambda sol: 0*sol,
        lambda sol: torch.zeros((sol.shape[0], sol.shape[0]))]

    def forc_fun(xx):
        amp, scale = 1.0, 0.1
        loc = torch.tensor([0.25, 0.75])[:, None]
        return amp*torch.exp(
            -torch.sum((torch.as_tensor(xx)-loc)**2/scale**2, axis=0))[:, None]

    bndry_conds = [
        [zeros_fun_axis_1, "D"],
        [zeros_fun_axis_1, "D"],
        [zeros_fun_axis_1, "D"],
        [zeros_fun_axis_1, "D"]]

    diff_fun = partial(mesh.interpolate, kle(true_kle_params[:, None]))
    adj_solver = SteadyStateAdjointPDE(AdvectionDiffusionReaction(
        mesh, bndry_conds, diff_fun, vel_fun, react_funs[0], forc_fun,
        react_funs[1]), None)

    noise = np.random.normal(0, true_noise_std, (obs_indices.shape[0]))
    true_sol = adj_solver.solve()

    mesh.plot(true_sol[:, None], nplot_pts_1d=50)
    plt.plot(mesh.mesh_pts[0, obs_indices], mesh.mesh_pts[1, obs_indices], 'ko')
    plt.show()
    
    obs = true_sol[obs_indices] + noise
    functional = partial(loglike_functional, obs, obs_indices, true_noise_std)
    adj_solver._functional = functional
    set_params = partial(set_kle_diff_params, kle)

    # TODO add std to params list
    init_guess = (
        true_params[:, None] +
        np.random.normal(0, 1, (true_params.shape[0], 1)))
    errors = check_gradients(
        partial(fwd_solver_finite_difference_wrapper, adj_solver,
                functional, set_params),
        lambda p: adj_solver.compute_gradient(
            set_params, torch.as_tensor(p)[:, 0]).numpy(),
        init_guess.numpy(), plot=False,
        fd_eps=np.logspace(-13, 0, 14)[::-1])

    from pyapprox.optimization.pya_minimize import pyapprox_minimize
    def objective(p):
        # scioy will pass in 1D variable
        obj, jac = adj_solver.compute_gradient(
            set_params, torch.as_tensor(p), return_obj=True)
        if obj.ndim == 0:
            obj = torch.as_tensor([obj])
        print(p, obj.item())
        print(jac.numpy())
        return obj.numpy(), jac[0, :].numpy()
    opt_result = pyapprox_minimize(
        objective, init_guess, method="trust-constr", jac=True)

if __name__ == "__main__":
    np.random.seed(1)
    advection_diffusion()
