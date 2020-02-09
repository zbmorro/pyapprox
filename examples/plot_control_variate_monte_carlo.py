r"""
Control Variate Monte Carlo
===========================
This tutorial describes how to implement and deploy control variate Monte Carlo sampling to compute expectations of model output from multiple models. It shows how multi-level Monte Carlo and multi-fidelity Monte Carlo are both control variate techniques. And demonstrates how this understanding can be used to improve their efficiency.

Let :math:`f(\rv):\reals^d\to\reals` be a function of :math:`d` random variables :math:`\rv=[\rv_1,\ldots,\rv_d]^T` with joint density :math:`\pdf(\rv)`. Our goal is to compute the expectation of an approximation :math:`f_\alpha` of the function :math:`f`, e.g.

.. math::

   Q_\alpha = \int_{\reals^d} f_\alpha(\rv)\,\pdf(\rv)\,d\rv

The approximation :math:`f_\alpha` typically arises from the need to numerically compute :math:`f`. For example :math:`f` may be a functional of a finite element solution of a system of partial differential equations that cannot be solved analytically.


Monte Carlo
+++++++++++

We can approximate th integral :math:`Q` using Monte Carlo quadrature by drawing :math:`N` random samples of :math:`\rv` from :math:`\pdf` and evaluating the function at each of these samples to obtain the data pairs :math:`\{(\rv^{(n)},f^{(n)}_\alpha)\}_{n=1}^N`, where :math:`f^{(n)}=f_\alpha(\rv^{(n)})`

.. math::

   \hat{Q}_{\alpha,N}=N^{-1}\sum_{n=1}^N f^{(n)}_\alpha

The mean squared error (MSE) of this estimator can be expressed as

.. math::
   
   \begin{align}
   \mean{\left(\hat{Q}_{\alpha,N}-\mean{Q}\right)^2}&=\mean{\left(\hat{Q}_{\alpha,N}-\mean{\hat{Q}_{\alpha,N}}+\mean{\hat{Q}_{\alpha,N}}-\mean{Q}\right)^2}\\
   &=\mean{\left(\hat{Q}_{\alpha,N}-\mean{\hat{Q}_{\alpha,N}}\right)^2}+\mean{\left(\mean{\hat{Q}_{\alpha,N}}-\mean{Q}\right)^2}\\
   &\qquad\qquad+\mean{2\left(\hat{Q}_{\alpha,N}-\mean{\hat{Q}_{\alpha,N}}\right)\left(\mean{\hat{Q}_{\alpha,N}}-\mean{Q}\right)}\\
   &=\var{\hat{Q}_{\alpha,N}}+\left(\mean{\hat{Q}_{\alpha,N}}-\mean{Q}\right)^2
   \end{align}
   
Here we used that :math:`\hat{Q}_{\alpha,N}` is an unbiased estimator, i.e. :math:`\mean{\hat{Q}_{\alpha,N}}=\mean{Q}` so the third term on the second line is zero. Now using

.. math::

   \var{\hat{Q}_{\alpha,N}}=\var{N^{-1}\sum_{n=1}^N f^{(n)}_\alpha}=N^{-1}\sum_{n=1}^N \var{f^{(n)}_\alpha}=N^{-1}\var{Q_\alpha}

yields

.. math::

   \mean{\left(\hat{Q}_{\alpha}-\mean{Q}\right)^2}=\underbrace{N^{-1}\var{Q_\alpha}}_{I}+\underbrace{\left(\mean{\hat{Q}_{\alpha}}-\mean{Q}\right)^2}_{II}

From this expression we can see that the MSE can be decomposed into two terms;
a so called stochastic error (I) and a deterministic bias (II). The first term is the error between the variance in the Monte Carlo estimator due to using a finite number of samples. The second term is due to using an approximation of :math:`f`. These two errors should be balanced, however in the vast majority of all MC analyses a single model $f_\alpha$ is used and the choice of $\alpha$, e.g. mesh resolution, is made a priori without much concern for the balancing bias and variance. 

Given a fixed :math:`\alpha` the modelers only recourse to reducing the MSE is to reduce the variance of the estimator. In the following we plot the variance of the MC estimate of a simple algebraic function :math:`f_1` which belongs to an ensemble of models

.. math::
   :nowrap:

   \begin{align}
   f_0(\rv) &= A_0 \left(\rv_1^5\cos\theta_0 +\rv_2^5\sin\theta_0\right), \\
   f_1(\rv) &= A_1 \left(\rv_1^3\cos\theta_1 +\rv_2^3\sin\theta_1\right)+1/10,\\
   f_2(\rv) &= A_2 \left(\rv_1  \cos\theta_2 +\rv_2  \sin\theta_2\right)+1/5
   \end{align}


where :math:`\rv_1,\rv_2\sim\mathcal{U}(-1,1)` and all :math:`A` and :math:`\theta` coefficients are real. We choose to set :math:`A=\sqrt{11}`, :math:`A_1=\sqrt{7}` and :math:`A_2=\sqrt{3}` to obtain unitary variance for each model. This example can be tuned to change the correlation between the models in a systematic way (by varying :math:`\theta_1`. We will levarage this later in the tutorial.
"""

#%%
# Lets setup the problem
import pyapprox as pya
import numpy as np
import matplotlib.pyplot as plt
from pyapprox.tests.test_control_variate_monte_carlo import TunableExample
from scipy.stats import uniform

np.random.seed(1)
univariate_variables = [uniform(-1,2),uniform(-1,2)]
variable = pya.IndependentMultivariateRandomVariable(univariate_variables)
print(variable)
shifts=[.1,.2]
model = TunableExample(np.pi/3,shifts=shifts)

#%%
# Now let us compute the mean of :math:`f_1` using Monte Carlo
nsamples = int(1e2)
samples = pya.generate_independent_random_samples(
    variable,nsamples)
values = model.m2(samples) #m2=f_1(z) above
pya.print_statistics(samples,values)

#%%
# We can compute the exact mean using sympy and compute the MC MSE
import sympy as sp
z1,z2 = sp.Symbol('z1'),sp.Symbol('z2')
ranges = [-1,1,-1,1]
integrand_f1=model.A1*(sp.cos(model.theta1)*z1**3+sp.sin(model.theta1)*z2**3)+shifts[0]
exact_integral_f1 = float(
    sp.integrate(integrand_f1,(z1,ranges[0],ranges[1]),(z2,ranges[2],ranges[3])))

print('MSE=',(values.mean()-exact_integral_f1)**2)

#%%
#Now let us compute the MSE for different sample sets of the same size and plot
#the distribution of the MC estimator :math:`Q_{\alpha,N}`

ntrials=1000
means = np.empty(ntrials)
for ii in range(ntrials):
    samples = pya.generate_independent_random_samples(
        variable,nsamples)
    values = model.m2(samples)
    means[ii] = values.mean()
fig,ax = plt.subplots()
textstr = '\n'.join([r'$E[Q_{0,N}]=\textrm{%.2e}$'%means.mean(),
                     r'$V[Q_{0,N}]=\textrm{%.2e}$'%means.var()])
ax.hist(means,bins=ntrials//100,density=True)
_ = ax.text(0.6,0.75,textstr,transform=ax.transAxes)

#%%
#The numerical results match our theory. Specifically the estimator is unbiased( i.e. mean zero, and the variance of the estimator is :math:`\var{Q_{0,N}}=\var{Q_{0}}/N=1/N`.
#
#The variance of the estimator can be driven to zero by increasing the number of samples :math:`N`. However when the variance becomes less than the bias, i.e. :math:`\left(\mean{Q_{\alpha}-Q}\right)^2>\var{Q_{\alpha}}/N`, then the MSE will not decrease and any further samples used to reduce the variance are wasted.
#
#Let our true model be :math:`f_0` above. The following code compues the bias induced by using $f_\alpha=f_1$ and also plots the contours of :math:`f_0(\rv)-f_1(\rv)`.

integrand_f0 = model.A0*(sp.cos(model.theta0)*z1**5+sp.sin(model.theta0)*z2**5)
exact_integral_f0 = float(
    sp.integrate(integrand_f0,(z1,ranges[0],ranges[1]),(z2,ranges[2],ranges[3])))
bias = (exact_integral_f0-exact_integral_f1)**2
print('bias=',bias)

fig,ax = plt.subplots()
X,Y,Z = pya.get_meshgrid_function_data(
    lambda z: model.m1(z)-model.m2(z),[-1,1,-1,1],50)
cset = ax.contourf(X, Y, Z, levels=np.linspace(Z.min(),Z.max(),20))
_ = plt.colorbar(cset,ax=ax)
plt.show()

#%%
# So using :math:`N=7` samples the bias exceeds the variance and the MSE cannot be reduced by adding more samples. To reduce the MSE we must reduce the bias. We can do this by leveraging additional models.

#%%
# Control-variate Monte Carlo (CVMC)
# ++++++++++++++++++++++++++++++++++
#
# .. math::
#
#    \tilde{Q}_{\V{\alpha}}^{\text{CV}} = \tilde{Q}_{\V{\alpha}} + \gamma \left( \tilde{Q}_{\V{\kappa}} - \mu_{\V{\kappa}} \right) 
#
# .. math::
#   
#    \var{\tilde{Q}_{\V{\alpha}}^{\text{CV}}}=(1-\frac{r_\V{\kappa}-r_\V{\alpha}}{r_\V{\kappa}r_\V{\alpha}}\rho^2)\var{\tilde{Q}_{\V{\alpha}}}
#
# Multi-level Monte Carlo (MLMC)
# ++++++++++++++++++++++++++++++
#
# Total cost is
#
# .. math::
#
#    C_{\mathrm{tot}}=\sum_{l=1}^L C_lr_lN_1
#   
# Variance of estimator is
#
# .. math::
#  
#    \var{\hat{Q}_L}=\sum_{l=1}^L \var{Y_l}r_lN_1
#   
# Treating :math:`r_l` as a continuous variable the variance of the MLMC estimator is minimized for a fixed budget :math:`C` by setting
#
# .. math::
#
#    N_l=r_lN_1=\sqrt{\var{Y_l}/C_l}
#   
# Choose L so that
#
# .. math::
#   
#    \left(\mean{\hat{Q}_L}-\mean{Q}\right)^2<\frac{1}{2}\epsilon^2
#   
# Choose :math:`N_l` so total variance
#
# .. math::
#    \var{\hat{Q}_L}<\frac{1}{2}\epsilon^2
#
# Multi-fidelity Monte Carlo (MFMC)
# +++++++++++++++++++++++++++++++++
#
# .. math::
#   
#    r_i=\left(\frac{C_1(\rho^2_{1i}-\rho^2_{1i+1})}{C_i(1-\rho^2_{12})}\right)^{\frac{1}{2}}
#   
# Let :math:`C=(C_1\cdots C_L)^T r=(r_1\cdots r_L)^T` then
#
# .. math::
#    :nowrap:
#   
#    \begin{align} N_1=\frac{C_{\mathrm{tot}}}{C^Tr} & & N_i=r_iN_1\end{align}
#  
# The control variate weights are
#
# .. math::
#   
#    \alpha_i=\frac{\rho_{1i}\sigma_1}{\sigma_i}
