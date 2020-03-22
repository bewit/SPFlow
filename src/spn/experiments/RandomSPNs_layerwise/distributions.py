import logging

import torch
from torch import distributions as dist
from torch import nn
from torch.nn import functional as F

from spn.algorithms.layerwise.distributions import Leaf, dist_forward
from spn.algorithms.layerwise.layers import Product
from spn.algorithms.layerwise.type_checks import check_valid

logger = logging.getLogger(__name__)


class RatNormal(Leaf):
    """ Implementation as in RAT-SPN

    Gaussian layer. Maps each input feature to its gaussian log likelihood."""

    def __init__(
        self,
        multiplicity: int,
        in_features: int,
        dropout: float = 0.0,
        min_sigma: float = 0.1,
        max_sigma: float = 1.0,
        min_mean: float = None,
        max_mean: float = None,
    ):
        """Creat a gaussian layer.

        Args:
            multiplicity: Number of parallel representations for each input feature.
            in_features: Number of input features.

        """
        super().__init__(multiplicity, in_features, dropout)

        # Create gaussian means and stds
        self.means = nn.Parameter(torch.randn(1, in_features, multiplicity))
        self.stds = nn.Parameter(torch.rand(1, in_features, multiplicity))

        self.min_sigma = check_valid(min_sigma, float, 0.0, max_sigma)
        self.max_sigma = check_valid(max_sigma, float, min_sigma)
        self.min_mean = check_valid(min_mean, float, upper_bound=max_mean, allow_none=True)
        self.max_mean = check_valid(max_mean, float, min_mean, allow_none=True)

    def _get_base_distribution(self) -> torch.distributions.Distribution:
        if self.min_sigma < self.max_sigma:
            sigma_ratio = torch.sigmoid(self.stds)
            sigma = self.min_sigma + (self.max_sigma - self.min_sigma) * sigma_ratio
        else:
            sigma = 1.0

        means = self.means
        if self.max_mean:
            assert self.min_mean is not None
            mean_range = self.max_mean - self.min_mean
            means = torch.sigmoid(self.means) * mean_range + self.min_mean

        gauss = dist.Normal(means, torch.sqrt(sigma))
        return gauss


class IndependentMultivariate(Leaf):
    def __init__(
        self,
        multiplicity: int,
        in_features: int,
        cardinality: int,
        dropout: float = 0.0,
        leaf_base_class: Leaf = RatNormal,
    ):
        """
        Create multivariate distribution that only has non zero values in the covariance matrix on the diagonal.

        Args:
            multiplicity: Number of parallel representations for each input feature.
            cardinality: Number of variables per gauss.
            in_features: Number of input features.
            dropout: Dropout probabilities.
            leaf_base_class (Leaf): The encapsulating base leaf layer class.
        
        """
        super(IndependentMultivariate, self).__init__(multiplicity, in_features, dropout)
        self.base_leaf = leaf_base_class(multiplicity=multiplicity, in_features=in_features, dropout=dropout)
        self.prod = Product(in_features=in_features, cardinality=cardinality)
        self._pad = (cardinality - self.in_features % cardinality) % cardinality

        self.cardinality = check_valid(cardinality, int, 2, in_features + 1)
        self.out_shape = f"(N, {self.prod._out_features}, {multiplicity})"

    def _init_weights(self):
        if isinstance(self.base_leaf, RatNormal):
            truncated_normal_(self.base_leaf.stds, std=0.5)

    def forward(self, x: torch.Tensor):
        x = self.base_leaf(x)

        if self._pad:
            # Pad marginalized node
            x = F.pad(x, pad=[0, 0, 0, self._pad], mode="constant", value=0.0)

        x = self.prod(x)
        return x

    def _get_base_distribution(self):
        raise Exception("IndependentMultivariate does not have an explicit PyTorch base distribution.")

    def sample(self, indices: torch.Tensor = None, evidence: torch.Tensor=None, n: int = None) -> torch.Tensor:
        # TODO: maybe check padding?
        indices = self.prod.sample(indices=indices, n=n)
        samples = self.base_leaf.sample(indices=indices, n=n)
        return samples

    def __repr__(self):
        return f"IndependentMultivariate(in_features={self.in_features}, multiplicity={self.multiplicity}, dropout={self.dropout}, cardinality={self.cardinality}, out_shape={self.out_shape})"


def truncated_normal_(tensor, mean=0, std=0.1):
    """
    Truncated normal from https://discuss.pytorch.org/t/implementing-truncated-normal-initializer/4778/15
    """
    size = tensor.shape
    tmp = tensor.new_empty(size + (4,)).normal_()
    valid = (tmp < 2) & (tmp > -2)
    ind = valid.max(-1, keepdim=True)[1]
    tensor.data.copy_(tmp.gather(-1, ind).squeeze(-1))
    tensor.data.mul_(std).add_(mean)
