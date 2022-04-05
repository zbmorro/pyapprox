"""
Multivariate Lagrange Interpolation
-----------------------------------
For smooth function Lagrange polynomials can be used to
interpolate univariate functions evaluated on a tensor-product grid.
Pyapprox uses the Barycentric formulation of Lagrange interpolation
which is more efficient and stable than traditional Lagrange interpolation.

We must define the univariate grids that we will use to construct the
tensor product grid. While technically Lagrange interpolation can be used with
any 1D grids, it is better to use points well suited to polynomial
interpolation. Here we use the samples of a Gaussian quadrature rule.
"""
import pyapprox as pya
from scipy import stats
import numpy as np
from functools import partial

degree = 10
scipy_vars = [stats.uniform(-1, 2), stats.uniform(-1, 2)]
grid_samples_1d = [pya.get_univariate_gauss_quadrature_rule_from_variable(
    rv, degree)[0] for rv in scipy_vars]


#%%
#Now lets define the function we want to interpolate, e.g.
#:math:`f(\rv)=\rv_1^2+\rv_2^2`
def fun(samples):
    return np.sum(samples**2, axis=0)[:, None]


##%
#Now we will use partial to create a callable function that just takes
#the samples at which we want to evaluate the interpolant
#This function will evaluate fun on a tensor product grid internally
interp_fun = partial(pya.tensor_product_barycentric_lagrange_interpolation,
                     grid_samples_1d, fun)

variable = pya.IndependentRandomVariable(scipy_vars)
X, Y, Z = pya.get_meshgrid_function_data_from_variable(
    interp_fun, variable, 50)
fig, ax = pya.plt.subplots(1, 1, figsize=(8, 6))
ax.contourf(X, Y, Z, levels=np.linspace(Z.min(), Z.max(), 20))
pya.plot_2d_samples(
    pya.cartesian_product(grid_samples_1d), ax, marker='o', c='r')
pya.plt.show()

#%%
#Barycentric interpolation can be used for any number of variables. However,
#the number of evaluations of the target function grows exponentially with
#the number of variables
