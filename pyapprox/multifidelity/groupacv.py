from functools import partial
from itertools import combinations

import torch
import numpy as np
from scipy.optimize import minimize

try:
    import cvxpy
    _cvx_available = True
except ImportError:
    _cvx_available = False


from pyapprox.surrogates.autogp._torch_wrappers import (
    full, multidot, pinv, solve, hstack, vstack, asarray, torch,
    eye, log, einsum, floor, arange)


def _restriction_matrix(ncols, subset):
    # TODO Consider replacing _restriction_matrix.T.dot(A) with
    # special indexing applied to A
    nsubset = len(subset)
    mat = np.zeros((nsubset, ncols))
    for ii in range(nsubset):
        mat[ii, subset[ii]] = 1.0
    return mat


def get_model_subsets(nmodels, max_subset_nmodels=None):
    """
    Parameters
    ----------
    nmodels : integer
        The number of models

    max_subset_nmodels : integer
        The maximum number of in a subset.
    """
    if max_subset_nmodels is None:
        max_subset_nmodels = nmodels
    assert max_subset_nmodels > 0
    assert max_subset_nmodels <= nmodels
    subsets = []
    model_indices = np.arange(nmodels)
    for nsubset_lfmodels in range(1, max_subset_nmodels+1):
        for subset_indices in combinations(
                model_indices, nsubset_lfmodels):
            idx = np.asarray(subset_indices).astype(int)
            subsets.append(idx)
    return subsets


def _get_allocation_matrix_is(subsets):
    nsubsets = len(subsets)
    npartitions = nsubsets
    allocation_mat = full(
        (nsubsets, npartitions), 0., dtype=torch.double)
    for ii, subset in enumerate(subsets):
        allocation_mat[ii, ii] = 1.0
    return allocation_mat


def _get_allocation_matrix_nested(subsets):
    # nest partitions according to order of subsets
    nsubsets = len(subsets)
    npartitions = nsubsets
    allocation_mat = full(
        (nsubsets, npartitions), 0., dtype=torch.double)
    for ii, subset in enumerate(subsets):
        allocation_mat[ii, :ii+1] = 1.0
    return allocation_mat


def _nest_subsets(subsets, nmodels):
    for subset in subsets:
        if np.allclose(subset, [0]):
            raise ValueError("Cannot use subset [0]")
    idx = sorted(
        list(range(len(subsets))),
        key=lambda ii: (len(subsets[ii]), tuple(nmodels-subsets[ii])),
        reverse=True)
    return [subsets[ii] for ii in idx], np.array(idx)


def _grouped_acv_beta(nmodels, Sigma, subsets, R, reg, asketch):
    """
    Parameters
    ----------
    nmodels: integer
        The total number of models including the highest fidelity

    Sigma : array (nestimators, nestimators)
        The covariance between all estimators

    reg : float
        Regularization parameter to stabilize matrix inversion
    """
    reg_mat = np.identity(nmodels)*reg
    if asketch.shape != (nmodels, 1):
        raise ValueError("asketch has the wrong shape")

    # TODO instead of applyint R matrices just collect correct rows and columns
    beta = multidot((
        pinv(Sigma), R.T,
        solve(multidot((R, pinv(Sigma), R.T))+reg_mat, asketch[:, 0])))
    return beta


def _grouped_acv_variance(nmodels, Sigma, subsets, R, reg, asketch):
    reg_mat = np.identity(nmodels)*reg
    if asketch.shape != (nmodels, 1):
        raise ValueError("asketch has the wrong shape")

    reg_mat = eye(nmodels)*reg
    return asketch.T @ pinv(multidot((R, pinv(Sigma), R.T))+reg_mat) @ asketch


def _grouped_acv_estimate(
        nmodels, Sigma, reg, subsets, subset_values, R, asketch):
    nsubsets = len(subsets)
    beta = _grouped_acv_beta(nmodels, Sigma, subsets, R, reg, asketch)
    ll, mm = 0, 0
    acv_mean = 0
    for kk in range(nsubsets):
        mm += len(subsets[kk])
        if subset_values[kk].shape[0] > 0:
            subset_mean = subset_values[kk].mean(axis=0)
            acv_mean += (beta[ll:mm]) @ subset_mean
        ll = mm
    return acv_mean


def _grouped_acv_sigma_block(
        subset0, subset1, nsamples_intersect, nsamples_subset0,
        nsamples_subset1, cov):
    nsubset0 = len(subset0)
    nsubset1 = len(subset1)
    block = full((nsubset0, nsubset1), 0.)
    if (nsamples_subset0*nsamples_subset1) == 0:
        return block
    block = cov[np.ix_(subset0, subset1)]*nsamples_intersect/(
                nsamples_subset0*nsamples_subset1)
    return block


def _grouped_acv_sigma(
        nmodels, nsamples_intersect, cov, subsets):
    nsubsets = len(subsets)
    Sigma = [[None for jj in range(nsubsets)] for ii in range(nsubsets)]
    for ii, subset0 in enumerate(subsets):
        N_ii = nsamples_intersect[ii, ii]
        Sigma[ii][ii] = _grouped_acv_sigma_block(
            subset0, subset0, N_ii, N_ii, N_ii, cov)
        for jj, subset1 in enumerate(subsets[:ii]):
            N_jj = nsamples_intersect[jj, jj]
            Sigma[ii][jj] = _grouped_acv_sigma_block(
                subset0, subset1, nsamples_intersect[ii, jj],
                N_ii, N_jj, cov)
            Sigma[jj][ii] = Sigma[ii][jj].T
    Sigma = vstack([hstack(row) for row in Sigma])
    return Sigma


class GroupACVEstimator():
    def __init__(self, stat, costs, cov, reg_blue=1e-12, subsets=None,
                 est_type="is", asketch=None):
        self._cov, self._costs = self._check_cov(cov, costs)
        self.nmodels = len(costs)
        self._reg_blue = reg_blue
        self._stat = stat

        self.subsets, self.allocation_mat = self._set_subsets(
            subsets, est_type)
        self.nsubsets = len(self.subsets)
        self.npartitions = self.allocation_mat.shape[1]
        self.partitions_per_model = self._get_partitions_per_model()
        self.partitions_intersect = (
            self._get_subset_intersecting_partitions())
        self.R = hstack(
            [asarray(_restriction_matrix(self.nmodels, subset).T)
             for ii, subset in enumerate(self.subsets)])
        self._costs = asarray(costs)
        self._cov = asarray(cov)
        self.subset_costs = self._get_model_subset_costs(
            self.subsets, self._costs)

        # set npatition_samples above small constant,
        # otherwise gradient will not be defined.
        self._npartition_samples_lb = 0  # 1e-5

        self._npartitions = self.nsubsets  # TODO replace .nsubsets everywhere
        self._optimized_criteria = None
        self._asketch = self._validate_asketch(asketch)

    def _check_cov(self, cov, costs):
        if cov.shape[0] != len(costs):
            print(cov.shape, costs.shape)
            raise ValueError("cov and costs are inconsistent")
        return cov.copy(), np.array(costs)

    def _set_subsets(self, subsets, est_type):
        if subsets is None:
            subsets = get_model_subsets(self.nmodels)
        if est_type == "is":
            get_allocation_mat = _get_allocation_matrix_is
        elif est_type == "nested":
            for ii, subset in enumerate(subsets):
                if np.allclose(subset, [0]):
                    del subsets[ii]
                    break
            subsets = _nest_subsets(subsets, self.nmodels)[0]
            get_allocation_mat = _get_allocation_matrix_nested
        else:
            raise ValueError(
                "incorrect est_type {0} specified".format(est_type))
        return subsets,  get_allocation_mat(subsets)

    def _get_partitions_per_model(self):
        # assume npartitions = nsubsets
        npartitions = self.allocation_mat.shape[1]
        partitions_per_model = full((self.nmodels, npartitions), 0.)
        for ii, subset in enumerate(self.subsets):
            partitions_per_model[
                np.ix_(subset, self.allocation_mat[ii] == 1)] = 1
        return partitions_per_model

    def _compute_nsamples_per_model(self, npartition_samples):
        nsamples_per_model = einsum(
            "ji,i->j", self.partitions_per_model, npartition_samples)
        return nsamples_per_model

    def _estimator_cost(self, npartition_samples):
        return sum(
            self._costs*self._compute_nsamples_per_model(npartition_samples))

    def _get_subset_intersecting_partitions(self):
        amat = self.allocation_mat
        npartitions = self.allocation_mat.shape[1]
        partition_intersect = full(
            (self.nsubsets, self.nsubsets, npartitions), 0.)
        for ii, subset_ii in enumerate(self.subsets):
            for jj, subset_jj in enumerate(self.subsets):
                # partitions are shared when sum of allocation entry is 2
                partition_intersect[ii, jj, amat[ii]+amat[jj] == 2] = 1.
        return partition_intersect

    def _nintersect_samples(self, npartition_samples):
        """
        Get the number of samples in the intersection of two subsets.

        Note the number of samples per subset is simply the diagonal of this
        matrix
        """
        return einsum(
            "ijk,k->ij", self.partitions_intersect, npartition_samples)

    def _sigma(self, npartition_samples):
        return _grouped_acv_sigma(
            self.nmodels, self._nintersect_samples(npartition_samples),
            self._cov, self.subsets)

    def _covariance_from_npartition_samples(self, npartition_samples):
        return _grouped_acv_variance(
            self.nmodels, self._sigma(npartition_samples), self.subsets,
            self.R, self._reg_blue, self._asketch)

    def _objective(self, npartition_samples_np, return_grad=True):
        npartition_samples = torch.as_tensor(
            npartition_samples_np, dtype=torch.double)
        if return_grad:
            npartition_samples.requires_grad = True
        est_var = self._covariance_from_npartition_samples(
            npartition_samples)
        # if using log must use exp in get_variance
        log_est_var = log(est_var)
        if not return_grad:
            return log_est_var.item()
        log_est_var.backward()
        grad = npartition_samples.grad.detach().numpy().copy()
        npartition_samples.grad.zero_()
        return log_est_var.item(), grad

    @staticmethod
    def _get_model_subset_costs(subsets, costs):
        subset_costs = np.array(
            [costs[subset].sum() for subset in subsets])
        return subset_costs

    def _cost_constraint(
            self, npartition_samples_np, target_cost, return_grad=False):
        # because this is a constraint it must only return grad or val
        # not both unlike usual PyApprox convention
        npartition_samples = torch.as_tensor(
            npartition_samples_np, dtype=torch.double)
        if return_grad:
            npartition_samples.requires_grad = True
        val = (target_cost-self._estimator_cost(npartition_samples))
        if not return_grad:
            return val.item()
        val.backward()
        grad = npartition_samples.grad.detach().numpy().copy()
        npartition_samples.grad.zero_()
        return grad

    def _nhf_samples(self, npartition_samples):
        return (self.partitions_per_model[0]*npartition_samples).sum()

    def _nhf_samples_constraint(self, npartition_samples, min_nhf_samples):
        return self._nhf_samples(npartition_samples)-min_nhf_samples

    def _nhf_samples_constraint_jac(self, npartition_samples):
        return self.partitions_per_model[0]

    def _get_constraints(self, target_cost, min_nhf_samples, constraint_reg=0):
        cons = [
            {'type': 'ineq',
             'fun': self._cost_constraint,
             # 'jac': partial(self._cost_constraint, return_grad=True),
             'args': (target_cost, )}]
        if min_nhf_samples > 0:
            cons += [
                {'type': 'ineq',
                 'fun': self._nhf_samples_constraint,
                 # 'jac': self._nhf_samples_constraint_jac,
                 'args': [min_nhf_samples]}]
        return cons

    def _constrained_objective(self, cons, x):
        # used for gradient free optimizers
        lamda = 1e12
        cons_term = 0
        for con in cons:
            c_val = con["fun"](x, *con["args"])
            if c_val < 0:
                cons_term -= c_val * lamda
        return self._objective(x, return_grad=False) + cons_term

    def _init_guess(self, target_cost):
        # start with the same number of samples per partition

        # get the number of samples per model when 1 sample is in each
        # partition
        nsamples_per_model = self._compute_nsamples_per_model(
            full((self.npartitions,), 1.))
        cost = (nsamples_per_model*self._costs).sum()

        # the total number of samples per partition is then target_cost/cost
        # we take the floor to make sure we do not exceed the target cost
        return full(
            (self.npartitions,), np.floor(target_cost/cost))

    def _init_guess1(self, target_cost):
        # the total number of samples per partition is then target_cost/cost
        # we take the floor to make sure we do not exceed the target cost
        init_guess = np.zeros(self.npartitions)
        init_guess[-1] = target_cost / self.subset_costs[-1]
        return init_guess

    def _update_init_guess(
            self, init_guess, target_cost, min_nhf_samples, constraint_reg):
        constraints = self._get_constraints(
            target_cost, min_nhf_samples, constraint_reg)
        method = "nelder-mead"
        options = {}
        options["xatol"] = 1e-5
        options["fatol"] = 1e-5
        options["maxfev"] = 100 * len(init_guess)
        obj = partial(self._constrained_objective, constraints)
        res = minimize(
            obj, init_guess, jac=False,
            method=method, constraints=None, options=options,
            bounds=None)
        return res.x

    def _set_optimized_params_base(self, rounded_npartition_samples,
                                   rounded_nsamples_per_model,
                                   rounded_target_cost):
        self._rounded_npartition_samples = rounded_npartition_samples
        self._rounded_nsamples_per_model = rounded_nsamples_per_model
        self._rounded_target_cost = rounded_target_cost
        self._opt_sample_splits = self._sample_splits_per_model()
        self._optimized_sigma = self._sigma(self._rounded_npartition_samples)
        self._optimized_criteria = self._covariance_from_npartition_samples(
            self._rounded_npartition_samples).item()

    def _set_optimized_params(self, npartition_samples, round_nsamples=True):
        if round_nsamples:
            rounded_npartition_samples = floor(npartition_samples)
        else:
            rounded_npartition_samples = npartition_samples
        self._set_optimized_params_base(
            rounded_npartition_samples,
            self._compute_nsamples_per_model(rounded_npartition_samples),
            self._estimator_cost(rounded_npartition_samples))

    def _get_bounds(self):
        # better to use bounds because they are never violated
        # but enforcing bounds as constraints means bounds can be violated
        bounds = [(0, np.inf) for ii in range(self.npartitions)]
        return bounds

    def _validate_asketch(self, asketch):
        if asketch is None:
            asketch = full((self.nmodels, 1), 0)
            asketch[0] = 1.0
        asketch = asarray(asketch)
        if asketch.shape[0] != self._costs.shape[0]:
            raise ValueError("aksetch has the wrong shape")
        if asketch.ndim == 1:
            asketch = asketch[:, None]
        return asketch

    def allocate_samples(self, target_cost,
                         constraint_reg=0, round_nsamples=True,
                         options={}, init_guess=None,
                         min_nhf_samples=1):
        """
        Parameters
        ----------
        """
        # jac = True
        jac = False  # hack because currently autogradients do not works
        # when npartition_samples[ii]== 0
        obj = partial(self._objective, return_grad=jac)
        constraints = self._get_constraints(
            target_cost, min_nhf_samples, constraint_reg)
        if init_guess is None:
            init_guess = self._init_guess(target_cost)
            # init_guess = self._update_init_guess(
            #     init_guess, target_cost, min_nhf_samples, constraint_reg)
        init_guess = np.maximum(init_guess, self._npartition_samples_lb)
        options_copy = options.copy()
        method = options_copy.pop("method", "trust-constr")
        res = minimize(
            obj, init_guess, jac=jac,
            method=method, constraints=constraints, options=options_copy,
            bounds=self._get_bounds())
        if not res.success:
            # msg = f"optimization not successful {res}"
            msg = "optimization not successful"
            print(msg)
            raise RuntimeError(msg)

        self._set_optimized_params(asarray(res["x"]), round_nsamples)

    @staticmethod
    def _get_partition_splits(npartition_samples):
        """
        Get the indices, into the flattened array of all samples/values,
        of each indpendent sample partition
        """
        splits = np.hstack(
            (0, np.cumsum(npartition_samples.numpy()))).astype(int)
        return splits

    def generate_samples_per_model(self, rvs):
        ntotal_independent_samples = self._rounded_npartition_samples.sum()
        partition_splits = self._get_partition_splits(
            self._rounded_npartition_samples)
        samples = rvs(ntotal_independent_samples)
        samples_per_model = []
        for ii in range(self.nmodels):
            active_partitions = np.where(self.partitions_per_model[ii])[0]
            samples_per_model.append(np.hstack([
                samples[:, partition_splits[idx]:partition_splits[idx+1]]
                for idx in active_partitions]))
        return samples_per_model

    def _sample_splits_per_model(self):
        # for each model get the sample splits in values_per_model
        # that correspond to each partition used in values_per_model.
        # If the model is not evaluated for a partition, then
        # the splits will be [-1, -1]
        partition_splits = self._get_partition_splits(
            self._rounded_npartition_samples)
        splits_per_model = []
        for ii in range(self.nmodels):
            active_partitions = np.where(self.partitions_per_model[ii])[0]
            splits = np.full((self.npartitions, 2), -1, dtype=int)
            lb, ub = 0, 0
            for ii, idx in enumerate(active_partitions):
                ub += partition_splits[idx+1]-partition_splits[idx]
                splits[idx] = [lb, ub]
                lb = ub
            splits_per_model.append(splits)
        return splits_per_model

    def _separate_values_per_model(self, values_per_model):
        if len(values_per_model) != self.nmodels:
            msg = "len(values_per_model) {0} != nmodels {1}".format(
                len(values_per_model), self.nmodels)
            raise ValueError(msg)
        for ii in range(self.nmodels):
            if (values_per_model[ii].shape[0] !=
                    self._rounded_nsamples_per_model[ii]):
                msg = "{0} != {1}".format(
                    "len(values_per_model[{0}]): {1}".format(
                        ii, values_per_model[ii].shape[0]),
                    "nsamples_per_model[ii]: {0}".format(
                        self._rounded_nsamples_per_model[ii]))
                raise ValueError(msg)

        values_per_subset = []
        for ii, subset in enumerate(self.subsets):
            values = []
            active_partitions = np.where(self.allocation_mat[ii])[0]
            for model_id in subset:
                splits = self._opt_sample_splits[model_id]
                values.append(np.vstack([
                    values_per_model[model_id][
                        splits[idx, 0]:splits[idx, 1], :]
                    for idx in active_partitions]))
            values_per_subset.append(np.hstack(values))
        return values_per_subset

    def _estimate(self, values_per_subset):
        return _grouped_acv_estimate(
            self.nmodels, self._optimized_sigma, self._reg_blue, self.subsets,
            values_per_subset, self.R, self._asketch)

    def __call__(self, values_per_model):
        values_per_subset = self._separate_values_per_model(values_per_model)
        return self._estimate(values_per_subset)

    def _reduce_model_sample_splits(
            self, model_id, partition_id, nsamples_to_reduce):
        """ return splits that occur when removing the last N samples of
        a partition of a given model"""
        lb, ub = self._opt_sample_splits[model_id][partition_id]
        sample_splits = self._opt_sample_splits[model_id].copy()
        sample_splits[partition_id][1] = (ub-nsamples_to_reduce)
        for ii in range(partition_id+1, self.npartitions):
            sample_splits[ii] -= nsamples_to_reduce
        return sample_splits

    def _remove_pilot_samples(self, npilot_samples, samples_per_model):
        active_hf_subsets = np.where(self.partitions_per_model[0] == 1)[0]
        partition_id = active_hf_subsets[np.argmax(
            self._rounded_npartition_samples[active_hf_subsets])]
        active_partitions = np.where(self.allocation_mat[partition_id])[0]
        for model_id in self.subsets[partition_id]:
            if (npilot_samples + samples_per_model[model_id] >
                    self._rounded_npartition_samples[partition_id]):
                raise ValueError("Too many pilot values")
            splits = self._reduce_model_sample_splits(
                model_id, partition_id, npilot_samples)
            samples_per_model[model_id] = np.hstack(
                [samples_per_model[model_id][splits[idx, 0]: splits[idx, 1]]
                 for idx in active_partitions])
        return samples_per_model

    def insert_pilot_values(self, pilot_values, values_per_model):
        new_values_per_model = []
        active_hf_subsets = np.where(self.partitions_per_model[0] == 1)[0]
        partition_id = active_hf_subsets[np.argmax(
            self._rounded_npartition_samples[active_hf_subsets])]
        for model_id in self.subsets[partition_id]:
            npilot_values = pilot_values[model_id].shape[0]
            if npilot_values != pilot_values[0]:
                msg = "Must have the same number of pilot values "
                msg += "for each model"
                raise ValueError(msg)
            if (npilot_values + values_per_model[model_id] >
                    self._rounded_npartition_samples[partition_id]):
                raise ValueError("Too many pilot values")
            lb, ub = self._opt_sample_splits[model_id][partition_id]
            ub -= npilot_values
            # add back the pilot samples to the end of the samples of
            # the partition with partition_id
            values_per_model[model_id] = np.vstack((
                values_per_model[model_id][:ub], pilot_values[model_id],
                values_per_model[model_id][ub:]))
        return new_values_per_model

    def __repr__(self):
        if self._optimized_criteria is None:
            return "{0}()".format(
                self.__class__.__name__)
        rep = "{0}(criteria={1:.3g}".format(
            self.__class__.__name__, self._optimized_criteria)
        rep += " target_cost={0:.5g}, nsamples={1})".format(
            self._rounded_target_cost,
            self._rounded_nsamples_per_model)
        return rep


class MLBLUEEstimator(GroupACVEstimator):
    def __init__(self, stats, costs, cov, reg_blue=1e-12, subsets=None,
                 asketch=None):
        # Currently stats is ignored.
        super().__init__(stats, costs, cov, reg_blue, subsets, est_type="is",
                         asketch=asketch)
        self._hf_subset_vec = self._get_nhf_subset_vec()

        # compute psi blocks once and store because they are independent
        # of the number of samples per partition/subset
        self._psi_blocks = self._compute_psi_blocks()
        self._psi_blocks_flat = np.hstack(
                [b.flatten()[:, None] for b in self._psi_blocks])

    def _compute_psi_blocks(self):
        submats = []
        for ii, subset in enumerate(self.subsets):
            R = _restriction_matrix(self.nmodels, subset)
            submat = np.linalg.multi_dot((
                R.T,
                np.linalg.pinv(self._cov[np.ix_(subset, subset)]),
                R))
            submats.append(submat)
        return submats

    def _psi_matrix(self, npartition_samples_np):
        return (self._psi_blocks_flat@npartition_samples_np).reshape(
            (self.nmodels, self.nmodels))
        # Psi = cvxpy.reshape(Psi, (self.nmodels, self.nmodels))
        # psi = np.identity(self.nmodels)*self._reg_blue
        # for ii, submat in enumerate(self._psi_blocks):
        #     psi += npartition_samples_np[ii]*submat
        # return psi

    def _objective(self, npartition_samples_np, return_grad=True):
        # leverage block diagonal structure to compute gradients efficiently
        psi = self._psi_matrix(npartition_samples_np)
        psi_inv = np.linalg.inv(psi)
        variance = np.linalg.multi_dot(
            (self._asketch.T, psi_inv, self._asketch))
        if not return_grad:
            return variance
        aT_psi_inv = self._asketch.T.numpy().dot(psi_inv)
        grad = np.array(
            [-np.linalg.multi_dot((aT_psi_inv, smat, aT_psi_inv.T))[0, 0]
             for smat in self._psi_blocks])
        return variance, grad

    def _cvxpy_psi(self, nsps_cvxpy):
        Psi = self._psi_blocks_flat@nsps_cvxpy
        Psi = cvxpy.reshape(Psi, (self.nmodels, self.nmodels))
        return Psi

    def _cvxpy_spd_constraint(self, nsps_cvxpy, t_cvxpy):
        Psi = self._cvxpy_psi(nsps_cvxpy)
        mat = cvxpy.bmat(
            [[Psi, self._asketch],
             [self._asketch.T, cvxpy.reshape(t_cvxpy, (1, 1))]])
        return mat

    def _get_nhf_subset_vec(self):
        hf_subset_vec = np.zeros(self.nsubsets)
        for ii, subset in enumerate(self.subsets):
            if 0 in subset:
                hf_subset_vec[ii] = 1
        return hf_subset_vec

    def _minimize_cvxpy(self, target_cost, min_nhf_samples):
        # use notation from https://www.cvxpy.org/examples/basic/sdp.html

        t_cvxpy = cvxpy.Variable(nonneg=True)
        nsps_cvxpy = cvxpy.Variable(self.nsubsets, nonneg=True)
        obj = cvxpy.Minimize(t_cvxpy)
        constraints = [self.subset_costs@nsps_cvxpy <= target_cost]
        constraints += [self._hf_subset_vec@nsps_cvxpy >= min_nhf_samples]
        constraints += [self._cvxpy_spd_constraint(
            nsps_cvxpy, t_cvxpy) >> 0]
        prob = cvxpy.Problem(obj, constraints)
        prob.solve(verbose=0, solver="CVXOPT")
        res = dict([("x",  nsps_cvxpy.value), ("fun", t_cvxpy.value)])
        return res

    def allocate_samples(self, target_cost,
                         constraint_reg=0, round_nsamples=True,
                         options={}, init_guess=None, min_nhf_samples=1):
        options_copy = options.copy()
        method = options_copy.pop("method", "trust-constr")
        if method == "cvxpy":
            if not _cvx_available:
                raise ImportError("must install cvxpy")
            res = self._minimize_cvxpy(target_cost, min_nhf_samples)
            return self._set_optimized_params(
                asarray(res["x"]), round_nsamples)
        return super().allocate_samples(
            target_cost, constraint_reg, round_nsamples,
            options_copy, init_guess, min_nhf_samples)


#cvxpy requires cmake
#on osx with M1 chip install via
#arch -arm64 brew install cmake
#must also install cvxopt via
#pip install cvxopt
