"""
Testing the extreme value distributions
"""

import unittest
import numpy as np

from numpy.testing import assert_almost_equal
from hazard.evd import estimateEVD


class TestEvd(unittest.TestCase):

    def setUp(self):
        self.v = np.array([0., 0., 38.55, 41.12, 59.29, 61.75, 74.79])
        self.years = np.array([25.0, 50.0, 100.0, 250.0, 500.0, 2000.0])
        self.w0 = np.array([59.26156235, 69.34857941, 76.71388245,
                            84.10202789, 88.47135925, 95.00366974])
        self.loc0 = np.array([49.2291362594])
        self.scale0 = np.array([16.3463688259])
        self.shp0 = np.array([0.272970209861])
        self.missingValue = np.array(-9999.0)

    def testEVD(self):
        """Testing extreme value distribution"""
        w, loc, scale, shp = estimateEVD(self.v,
                                         self.years,
                                         missingValue=-9999,
                                         minRecords=3,
                                         yrspersim=10)

        assert_almost_equal(w, self.w0, decimal=5)
        assert_almost_equal(loc, self.loc0, decimal=5)
        assert_almost_equal(scale, self.scale0, decimal=5)
        assert_almost_equal(shp, self.shp0, decimal=5)

        w2, loc2, scale2, shp2 = estimateEVD(self.v,
                                             self.years,
                                             missingValue=-9999,
                                             minRecords=50,
                                             yrspersim=10)

        assert_almost_equal(w2, np.ones(6) * self.missingValue, decimal=5)
        assert_almost_equal(loc2, self.missingValue, decimal=5)
        assert_almost_equal(scale2, self.missingValue, decimal=5)
        assert_almost_equal(shp2, self.missingValue, decimal=5)

if __name__ == "__main__":
    suite = unittest.makeSuite(TestEvd, 'test')
    unittest.TextTestRunner().run(suite)
