import os
import sys
import unittest
import cPickle
import NumpyTestCase

from WindfieldInterface.windmodels import *

try:
    import pathLocate
except:
    from unittests import pathLocate

# Add parent folder to python path
unittest_dir = pathLocate.getUnitTestDirectory()
sys.path.append(pathLocate.getRootDirectory())



class TestWindProfile(NumpyTestCase.NumpyTestCase):

    def setUp(self):
        pkl_file = open(os.path.join(
            unittest_dir, 'test_data', 'windProfile_testdata.pck'), 'rb')
        self.R = cPickle.load(pkl_file)
        self.pEnv = cPickle.load(pkl_file)
        self.pCentre = cPickle.load(pkl_file)
        self.rMax = cPickle.load(pkl_file)
        self.cLat = cPickle.load(pkl_file)
        self.cLon = cPickle.load(pkl_file)
        self.beta = cPickle.load(pkl_file)
        self.rMax2 = cPickle.load(pkl_file)
        self.beta1 = cPickle.load(pkl_file)
        self.beta2 = cPickle.load(pkl_file)
        self.test_wP_rankine = cPickle.load(pkl_file)
        self.test_wP_jelesnianski = cPickle.load(pkl_file)
        self.test_wP_holland = cPickle.load(pkl_file)
        self.test_wP_willoughby = cPickle.load(pkl_file)
        self.test_wP_doubleHolland = cPickle.load(pkl_file)
        self.test_wP_powell = cPickle.load(pkl_file)
        pkl_file.close()

    def testRankine(self):
        profile = RankineWindProfile(
            self.cLat, self.cLon, self.pEnv, self.pCentre, self.rMax)
        V = profile.velocity(self.R)
        self.numpyAssertAlmostEqual(V, self.test_wP_rankine)

    def testJelesnianski(self):
        profile = JelesnianskiWindProfile(
            self.cLat, self.cLon, self.pEnv, self.pCentre, self.rMax)
        V = profile.velocity(self.R)
        self.numpyAssertAlmostEqual(V, self.test_wP_jelesnianski)

    def testHolland(self):
        profile = HollandWindProfile(self.cLat, self.cLon, self.pEnv,
                                     self.pCentre, self.rMax, self.beta)
        V = profile.velocity(self.R)
        self.numpyAssertAlmostEqual(V, self.test_wP_holland)

    def testWilloughby(self):
        profile = WilloughbyWindProfile(
            self.cLat, self.cLon, self.pEnv, self.pCentre, self.rMax)
        V = profile.velocity(self.R)
        self.numpyAssertAlmostEqual(V, self.test_wP_willoughby)

    def testPowell(self):
        profile = PowellWindProfile(
            self.cLat, self.cLon, self.pEnv, self.pCentre, self.rMax)
        V = profile.velocity(self.R)
        self.numpyAssertAlmostEqual(V, self.test_wP_powell)

    def testDoubleHolland(self):
        profile = DoubleHollandWindProfile(
            self.cLat, self.cLon, self.pEnv, self.pCentre, self.rMax,
            self.beta1, self.beta2, self.rMax2)
        V = profile.velocity(self.R)
        self.numpyAssertAlmostEqual(V, self.test_wP_doubleHolland)

if __name__ == "__main__":
    testSuite = unittest.makeSuite(TestWindProfile, 'test')
    unittest.TextTestRunner(verbosity=2).run(testSuite)
