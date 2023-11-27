import copy
from itertools import combinations
from functools import partial
from multiprocessing import Pool

import numpy as np

from pyapprox.multifidelity.acv import (
    CVEstimator, MLMCEstimator, MFMCEstimator, GMFEstimator, GISEstimator,
    GRDEstimator, MCEstimator, ACVEstimator, log_determinant_variance,
    log_trace_variance, determinant_variance)
from pyapprox.multifidelity.stats import (
    MultiOutputMean, MultiOutputVariance, MultiOutputMeanAndVariance,
    _nqoi_nqoi_subproblem)
from pyapprox.multifidelity.groupacv import MLBLUEEstimator


class BestEstimator():
    def __init__(self, est_types, stat_type, costs, cov,
                 max_nmodels, *est_args, **est_kwargs):
        
        self.best_est = None

        self._estimator_types = est_types
        self._stat_type = stat_type
        self._candidate_cov, self._candidate_costs = cov, np.asarray(costs)
        # self._ncandidate_nmodels is the number of total models
        self._ncandidate_models = len(self._candidate_costs)
        self._lf_model_indices = np.arange(1, self._ncandidate_models)
        self._nqoi = self._candidate_cov.shape[0]//self._ncandidate_models
        self._max_nmodels = max_nmodels
        self._args = est_args
        self._allow_failures = est_kwargs.get("allow_failures", False)
        if "allow_failures" in est_kwargs:
            del est_kwargs["allow_failures"]
        self._kwargs = est_kwargs
        self._best_model_indices = None
        self._all_model_labels = None

        self._save_candidate_estimators = False
        self._candidate_estimators = None

    @property
    def model_labels(self):
        return [self._all_model_labels[idx]
                for idx in self._best_model_indices]

    @model_labels.setter
    def model_labels(self, labels):
        self._all_model_labels = labels

    def _validate_kwargs(self, nsubset_lfmodels):
        sub_kwargs = copy.deepcopy(self._kwargs)
        if "recursion_index" in sub_kwargs:
            index = sub_kwargs["recursion_index"]
            if (np.allclose(index, np.arange(len(index))) or
                    np.allclose(index, np.zeros(len(index)))):
                sub_kwargs["recursion_index"] = index[:nsubset_lfmodels]
            else:
                msg = "model selection can only be used with recursion indices"
                msg += " (0, 1, 2, ...) or (0, ..., 0) or tree_depth is"
                msg += " not None"
                # There is no logical way to reduce a recursion index to use
                # a subset of model unless they are one of these two indices
                # or tree_depth is not None so that all possible recursion
                # indices are considered
                raise ValueError(msg)
        if "tree_depth" in sub_kwargs:
            sub_kwargs["tree_depth"] = min(
                sub_kwargs["tree_depth"], nsubset_lfmodels)
        return sub_kwargs

    def _get_estimator(self, est_type, subset_costs, subset_cov, 
                       target_cost, sub_args, sub_kwargs, allocate_kwargs):
        try:
            est = get_estimator(
                est_type, self._stat_type, self._nqoi,
                subset_costs, subset_cov, *sub_args, **sub_kwargs)
        except ValueError as e:
            if sub_kwargs.pop("verbosity", 0) > 0:
                print(e)
            # Some estimators, e.g. MFMC, fail when certain criteria
            # are not satisfied
            return None
        try:
            est.allocate_samples(target_cost, **allocate_kwargs)
            if sub_kwargs.pop("verbosity", 0) > 0:
                msg = "Model: {0} Objective: {1}".format(
                    idx, est._optimized_criteria.item())
                print(msg)
            return est
        except (RuntimeError, ValueError) as e:
            if self._allow_failures:
                return None
            raise e
        
    def _get_model_subset_estimator(
            self, qoi_idx, nsubset_lfmodels, allocate_kwargs,
            target_cost, lf_model_subset_indices):

        idx = np.hstack(([0], lf_model_subset_indices)).astype(int)
        subset_cov = _nqoi_nqoi_subproblem(
            self._candidate_cov, self._ncandidate_models, self._nqoi,
            idx, qoi_idx)
        subset_costs = self._candidate_costs[idx]
        sub_args = multioutput_stats[self._stat_type]._args_model_subset(
            self._ncandidate_models, self._nqoi, idx, *self._args)
        sub_kwargs = self._validate_kwargs(nsubset_lfmodels)

        best_est = None
        best_criteria = np.inf
        for est_type in self._estimator_types:
            est = self._get_estimator(
                est_type, subset_costs, subset_cov, 
                target_cost, sub_args, sub_kwargs, allocate_kwargs)
            if self._save_candidate_estimators:
                self._candidate_estimators.append(est)
            if est is not None and est._optimized_criteria < best_criteria:
                best_est = est
                best_criteria = est._optimized_criteria.item()
        return best_est
 
    def _get_best_model_subset_for_estimator_pool(
            self, nsubset_lfmodels, target_cost,
           best_criteria, best_model_indices, best_est, **allocate_kwargs):
        qoi_idx = np.arange(self._nqoi)
        nprocs = allocate_kwargs.get("nprocs", 1)
        pool = Pool(nprocs)
        indices = list(
            combinations(self._lf_model_indices, nsubset_lfmodels))
        result = pool.map(
            partial(self._get_model_subset_estimator,
                    qoi_idx, nsubset_lfmodels, allocate_kwargs,
                    target_cost), indices)
        pool.close()
        criteria = [
            np.array(est._optimized_criteria)
            if est is not None else np.inf for est in result]
        II = np.argmin(criteria)
        if not np.isfinite(criteria[II]):
            best_est = None
        else:
            best_est = result[II]
            best_model_indices = np.hstack(
                ([0], indices[II])).astype(int)
            best_criteria = best_est._optimized_criteria
        return best_criteria, best_model_indices, best_est

    def _get_best_model_subset_for_estimator_serial(
            self, nsubset_lfmodels, target_cost,
            best_criteria, best_model_indices, best_est, **allocate_kwargs):
        qoi_idx = np.arange(self._nqoi)
        for lf_model_subset_indices in combinations(
                self._lf_model_indices, nsubset_lfmodels):
            est = self._get_model_subset_estimator(
                qoi_idx, nsubset_lfmodels, allocate_kwargs,
                target_cost, lf_model_subset_indices)
            if est is not None and est._optimized_criteria < best_criteria:
                best_est = est
                best_model_indices = np.hstack(
                    ([0], lf_model_subset_indices)).astype(int)
                best_criteria = best_est._optimized_criteria
        return best_criteria, best_model_indices, best_est

    def _get_best_estimator(self, target_cost, **allocate_kwargs):
        best_criteria = np.inf
        best_est, best_model_indices = None, None
        nprocs = allocate_kwargs.get("nprocs", 1)
        
        if allocate_kwargs.get("verbosity", 0) > 0:
            print(f"Finding best model using {nprocs} processors")
        if "nprocs" in allocate_kwargs:
            del allocate_kwargs["nprocs"]

        if self._max_nmodels is None:
            min_nlfmodels = self._ncandidate_models-1
            max_nmodels = self._ncandidate_models
        else:
            min_nlfmodels = 1
            max_nmodels = self._ncandidate_models
            
        for nsubset_lfmodels in range(min_nlfmodels, max_nmodels):
            if nprocs > 1:
                 best_criteria, best_model_indices, best_est = (
                     self._get_best_model_subset_for_estimator_pool(
                         nsubset_lfmodels, target_cost,
                         best_criteria, best_model_indices, best_est,
                         **allocate_kwargs))
            else:
                 best_criteria, best_model_indices, best_est = (
                     self._get_best_model_subset_for_estimator_serial(
                         nsubset_lfmodels, target_cost,
                         best_criteria, best_model_indices, best_est,
                         **allocate_kwargs))
            
        if best_est is None:
            raise RuntimeError("No solutions found for any model subset")
        return best_est, best_model_indices

    def allocate_samples(self, target_cost, **allocate_kwargs):
        if self._save_candidate_estimators:
            self._candidate_estimators = []
        best_est, best_model_indices = self._get_best_estimator(
            target_cost, **allocate_kwargs)
        self.best_est = best_est
        self._best_model_indices = best_model_indices
        self._set_best_est_attributes()

    def _set_best_est_attributes(self):
        # allow direct access of important self.best_est attributes
        # __call__ cannot be set using this approach.
        attr_list = [
            # public functions
            "combine_acv_samples",
            "combine_acv_values",
            "generate_samples_per_model",
            "insert_pilot_values",
            "bootstrap",
            "plot_allocation",
            # private functions and variables
            "_separate_values_per_model",
            "_covariance_from_npartition_samples",
            "_covariance_from_partition_ratios",
            "_rounded_partition_ratios", "_stat",
            "_nmodels", "_cov", "_rounded_npartition_samples",
            "_rounded_nsamples_per_model", "_costs",
            "_optimized_criteria", "_get_discrepancy_covariances",
            "_rounded_target_cost",
            "_get_allocation_matrix",
            "_optimization_criteria",
            "_optimized_covariance",
            "_allocation_mat",
            "_npartitions"]
        for attr in attr_list:
            setattr(self, attr, getattr(self.best_est, attr))

    def __repr__(self):
        if self._optimized_criteria is None:
            return "{0}".format(self.__class__.__name__)
        return "{0}(est={1}, subset={2})".format(
            self.__class__.__name__, self.best_est, self._best_model_indices)

    def __call__(self, values):
        return self.best_est(values)


multioutput_estimators = {
    "cv": CVEstimator,
    "gmf": GMFEstimator,
    "gis": GISEstimator,
    "grd": GRDEstimator,
    "mfmc": MFMCEstimator,
    "mlmc": MLMCEstimator,
    "mc": MCEstimator,
    "mlblue": MLBLUEEstimator}


multioutput_stats = {
    "mean": MultiOutputMean,
    "variance": MultiOutputVariance,
    "mean_variance": MultiOutputMeanAndVariance,
}


def get_estimator(estimator_types, stat_type, nqoi, costs, cov, *stat_args,
                  max_nmodels=None, **est_kwargs):
    """
    Parameters
    ----------
    estimator_types : list [str] or str
        If str (or len(estimators_types==1), then return the estimator 
        named estimator_type (or estimator_types[0])

    stat_type : str
        The type of statistics to compute

    nqoi : integer
        The number of quantities of interest (QoI) that each model returns

    costs : np.ndarray (nmodels)
        The computational cost of evaluating each model

    cov : np.ndarray (nmodels*nqoi, nmodels*nqoi)
        The covariance between all the QoI of all the models

    stat_args : list or tuple
        The arguments that are needed to compute the statistic

    max_nmodels : integer
        If None, compute the estimator using all the models. If not None,
        find the model subset that uses at most max_nmodels that minimizes
        the estimator covariance.

    est_kwargs : dict
        Keyword arguments that will be passed when creating each estimator.
    """
    if isinstance(estimator_types, list) or max_nmodels is not None:
        if not isinstance(estimator_types, list):
            estimator_types = [estimator_types]
        return BestEstimator(
            estimator_types, stat_type, costs, cov,
            max_nmodels, *stat_args, **est_kwargs)

    if isinstance(estimator_types, list):
        estimator_type = estimator_types[0]
    else:
        estimator_type = estimator_types
    
    if estimator_type not in multioutput_estimators:
        msg = f"Estimator {estimator_type} not supported. "
        msg += f"Must be one of {multioutput_estimators.keys()}"
        raise ValueError(msg)

    if stat_type not in multioutput_stats:
        msg = f"Statistic {stat_type} not supported. "
        msg += f"Must be one of {multioutput_stats.keys()}"
        raise ValueError(msg)

    stat = multioutput_stats[stat_type](nqoi, cov, *stat_args)
    return multioutput_estimators[estimator_type](
        stat, costs, cov, **est_kwargs)


def _estimate_components(variable, est, funs, ii):
    """
    Notes
    -----
    To create reproducible results when running numpy.random in parallel
    must use RandomState. If not the results will be non-deterministic.
    This is happens because of a race condition. numpy.random.* uses only
    one global PRNG that is shared across all the threads without
    synchronization. Since the threads are running in parallel, at the same
    time, and their access to this global PRNG is not synchronized between
    them, they are all racing to access the PRNG state (so that the PRNG's
    state might change behind other threads' backs). Giving each thread its
    own PRNG (RandomState) solves this problem because there is no longer
    any state that's shared by multiple threads without synchronization.
    Also see new features
    https://docs.scipy.org/doc/numpy/reference/random/parallel.html
    https://docs.scipy.org/doc/numpy/reference/random/multithreading.html
    """
    random_state = np.random.RandomState(ii)
    samples_per_model = est.generate_samples_per_model(
        partial(variable.rvs, random_state=random_state))
    values_per_model = [
        fun(samples) for fun, samples in zip(funs, samples_per_model)]

    mc_est = est._stat.sample_estimate
    if ((isinstance(est, ACVEstimator) or isinstance(est, BestEstimator))):
        # the above condition does not allow BestEstimator to be
        # applied to CVEstimator
        est_val = est(values_per_model)
        acv_values = est._separate_values_per_model(values_per_model)
        Q = mc_est(acv_values[1])
        delta = np.hstack([mc_est(acv_values[2*ii]) -
                           mc_est(acv_values[2*ii+1])
                           for ii in range(1, est._nmodels)])
    elif isinstance(est, CVEstimator):
        est_val = est(values_per_model)
        Q = mc_est(values_per_model[0])
        delta = np.hstack(
            [mc_est(values_per_model[ii]) - est._lowfi_stats[ii-1]
             for ii in range(1, est._nmodels)])
    else:
        est_val = est(values_per_model[0])
        Q = mc_est(values_per_model[0])
        delta = Q*0
    return est_val, Q, delta


def _estimate_components_loop(
        variable, ntrials, est, funs, max_eval_concurrency):
    if max_eval_concurrency == 1:
        Q = []
        delta = []
        estimator_vals = []
        for ii in range(ntrials):
            est_val, Q_val, delta_val = _estimate_components(
                variable, est, funs, ii)
            estimator_vals.append(est_val)
            Q.append(Q_val)
            delta.append(delta_val)
        Q = np.array(Q)
        delta = np.array(delta)
        estimator_vals = np.array(estimator_vals)
        return estimator_vals, Q, delta

    # set flat funs to none so funs can be pickled
    pool = Pool(max_eval_concurrency)
    func = partial(_estimate_components, variable, est, funs)
    result = pool.map(func, list(range(ntrials)))
    pool.close()
    estimator_vals = np.asarray([r[0] for r in result])
    Q = np.asarray([r[1] for r in result])
    delta = np.asarray([r[2] for r in result])
    return estimator_vals, Q, delta


def numerically_compute_estimator_variance(
        funs, variable, est, ntrials=int(1e3), max_eval_concurrency=1,
        return_all=False):
    r"""
    Numerically estimate the variance of an approximate control variate
    estimator.

    Parameters
    ----------
    funs : list [callable]
        List of functions with signature

        `fun(samples) -> np.ndarray (nsamples, nqoi)`

    where samples has shape (nvars, nsamples)

    est : :class:`pyapprox.multifidelity.multioutput_monte_carlo.MCEstimator`
        A Monte Carlo like estimator for computing sample based statistics

    ntrials : integer
        The number of times to compute estimator using different randomly
        generated set of samples

    max_eval_concurrency : integer
        The number of processors used to compute realizations of the estimators
        which can be run independently and in parallel.

    Returns
    -------
    hf_covar_numer : np.ndarray (nstats, nstats)
        The estimator covariance of the single high-fidelity Monte Carlo
        estimator

    hf_covar : np.ndarray (nstats, nstats)
        The analytical value of the estimator covariance of the single
       high-fidelity Monte Carlo estimator


    covar_numer : np.ndarray (nstats, nstats)
        The estimator covariance of est

    hf_covar : np.ndarray (nstats, nstats)
        The analytical value of the estimator covariance of est

    est_vals : np.ndarray (ntrials, nstats)
        The values for the est for each trial. Only returned if return_all=True

    Q0 : np.ndarray (ntrials, nstats)
        The values for the single fidelity MC estimator for each trial.
        Only returned if return_all=True

    delta : np.ndarray (ntrials, nstats)
        The values for the differences between the low-fidelty estimators
        :math:`\mathcal{Z}_\alpha` and :math:`\mathcal{Z}_\alpha^*`
        for each trial. Only returned if return_all=True
    """
    ntrials = int(ntrials)
    est_vals, Q0, delta = _estimate_components_loop(
        variable, ntrials, est, funs, max_eval_concurrency)

    hf_covar_numer = np.cov(Q0, ddof=1, rowvar=False)
    hf_covar = est._stat.high_fidelity_estimator_covariance(
        est._rounded_npartition_samples[0])

    covar_numer = np.cov(est_vals, ddof=1, rowvar=False)
    covar = est._covariance_from_npartition_samples(
        est._rounded_npartition_samples).numpy()

    if not return_all:
        return hf_covar_numer, hf_covar, covar_numer, covar
    return hf_covar_numer, hf_covar, covar_numer, covar, est_vals, Q0, delta


def compare_estimator_variances(target_costs, estimators):
    """
    Compute the variances of different Monte-Carlo like estimators.

    Parameters
    ----------
    target_costs : np.ndarray (ntarget_costs)
        Different total cost budgets

    estimators : list (nestimators)
        List of Monte Carlo estimator objects, e.g.
        :class:`~pyapprox.multifidelity.multioutput_monte_carlo.MCEstimator`

    Returns
    -------
        optimized_estimators : list
         Each entry is a list of optimized estimators for a set of target costs
    """
    optimized_estimators = []
    for est in estimators:
        est_copies = []
        for target_cost in target_costs:
            est_copy = copy.deepcopy(est)
            est_copy.allocate_samples(target_cost)
            est_copies.append(est_copy)
        optimized_estimators.append(est_copies)
    return optimized_estimators


class ComparisionCriteria():
    def __init__(self, criteria_type):
        self._criteria_type = criteria_type

    def __call__(self, est_covariance, est):
        if self._criteria_type == "det":
            return determinant_variance(est_covariance)
        if self._criteria_type == "trace":
            return np.exp(log_trace_variance(est_covariance))
        raise ValueError(
            "Criteria {0} not supported".format(self._criteria_type))

    def __repr__(self):
        return "{0}(citeria={1})".format(
            self.__class__.__name__, self._criteria_type)


class SingleQoiAndStatComparisonCriteria(ComparisionCriteria):
    def __init__(self, stat_type, qoi_idx):
        """
        Compare estimators based on the variance of a single statistic
        for a single QoI even though mutiple QoI may have been used to compute
        multiple statistics

        Parameters
        ----------
        stat_type: str
            The stat type. Must be one of ["mean", "variance", "mean_variance"]

        qoi_idx: integer
            The index of the QoI as it appears in the covariance matrix
        """
        self._stat_type = stat_type
        self._qoi_idx = qoi_idx

    def __call__(self, est_covariance, est):
        if self._stat_type != "mean" and isinstance(
                est._stat, MultiOutputMeanAndVariance):
            return (
                est_covariance[est.nqoi+self._qoi_idx,
                               est._nqoi+self._qoi_idx])
        elif (isinstance(
                est._stat, (MultiOutputVariance, MultiOutputMean)) or
              self._stat_type == "mean"):
            return est_covariance[self._qoi_idx, self._qoi_idx]
        raise ValueError("{0} not supported".format(est._stat))

    def __repr__(self):
        return "{0}(stat={1}, qoi={2})".format(
            self.__class__.__name__, self._stat_type, self._qoi_idx)


def compute_variance_reductions(optimized_estimators,
                                criteria=ComparisionCriteria("det"),
                                nhf_samples=None):
    """
    Compute the variance reduction (relative to single model MC) for a
    list of optimized estimtors.

    Parameters
    ----------
    optimized_estimators : list
         Each entry is a list of optimized estimators for a set of target costs

    est_labels : list (nestimators)
        String used to label each estimator

    criteria : callable
        A function that returns a scalar metric of the estimator covariance
        with signature

        `criteria(cov) -> float`

        where cov is an np.ndarray (nstats, nstats) is the estimator covariance

    nhf_samples : int
        The number of samples of the high-fidelity model used for the
        high-fidelity only estimator. If None, then the number of high-fidelity
        evaluations that produce a estimator cost equal to the optimized
        target cost of the estimator is used. Usually, nhf_samples should be
        set to None.
    """
    var_red, est_criterias, sf_criterias = [], [], []
    optimized_estimators = optimized_estimators.copy()
    nestimators = len(optimized_estimators)
    for ii in range(nestimators):
        est = optimized_estimators[ii]
        est_criteria = criteria(est._covariance_from_npartition_samples(
            est._rounded_npartition_samples), est)
        if nhf_samples is None:
            nhf_samples = int(est._rounded_target_cost/est._costs[0])
        sf_criteria = criteria(
            est._stat.high_fidelity_estimator_covariance(
                nhf_samples), est)
        var_red.append(sf_criteria/est_criteria)
        sf_criterias.append(sf_criteria)
        est_criterias.append(est_criteria)
    return (np.asarray(var_red), np.asarray(est_criterias),
            np.asarray(sf_criterias))
