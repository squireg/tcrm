"""
:mod:`TrackGenerator` -- Tropical cyclone track generation
==========================================================

This module contains the core objects for tropical cyclone track generation.

Track generation can be run in parallel using MPI if the :term:`pypar` library
is found and TCRM is run using the :term:`mpirun` command. For example, to run
with 10 processors::

    mpirun -n 10 python main.py cairns.ini

:class:`TrackGenerator` can be correctly initialised and started by
calling the :meth: `run` with the location of a *configFile*::

    import TrackGenerator
    TrackGenerator.run('cairns.ini')

Alternatively, it can be run from the command line::

    python TrackGenerator.py cairns.ini

"""

import traceback

import os
import sys
import logging as log
import math
import random
import itertools
import numpy as np
import Utilities.stats as stats
import trackLandfall
import Utilities.nctools as nctools
import Utilities.Cmap as Cmap
import Utilities.Cstats as Cstats
import Utilities.maputils as maputils

from os.path import join as pjoin, dirname
from scipy.io.netcdf import netcdf_file
from config import ConfigParser
from StatInterface.generateStats import GenerateStats
from StatInterface.SamplingOrigin import SamplingOrigin
from Utilities.AsyncRun import AsyncRun
from Utilities.config import cnfGetIniValue
from Utilities.files import flStartLog, flLoadFile, flSaveFile
from Utilities.grid import SampleGrid
from MSLP.mslp_seasonal_clim import MSLPGrid
from Utilities.shptools import shpSaveTrackFile
from DataProcess.CalcFrequency import CalcFrequency


class TrackGenerator:
    """
    Generates tropical cyclone tracks based on the empirical probability
    distributions of speed, bearing, pressure and maximum radius.


    :type  processPath: string
    :param processPath: the location of the empirical probability
                        distribution files.

    :type  gridLimit: :class:`dict`
    :param gridLimit: the domain where the tracks will be generated.
                      The :class:`dict` should contain the keys :attr:`xMin`,
                      :attr:`xMax`, :attr:`yMin` and :attr:`yMax`. The *x*
                      variable bounds the latitude and the *y* variable bounds
                      the longitude.

    :type  gridSpace: :class:`dict`
    :param gridSpace: the grid spacing of the domain.
                      This is used to chop the domain into cells. The
                      :class:`dict` should contain the keys :attr:`x` and
                      :attr:`y`.

    :type  gridInc: :class:`dict`
    :param gridInc: the increments for the grid to be used by
                    :class:`StatInterface.GenerateStats` when insufficient
                    observations exist in the cell being analysed.

    :type  mslp: :class:`MSLPGrid` or :class:`SampleGrid`
    :param mslp: the MSLP grid to use.

    :type  landfall: :class:`LandfallDecay`
    :param landfall: the object that calculates the decay rate of a tropical
                     cyclone after it has made landfall. The object should have
                     the methods :meth:`onLand` and :meth:`pChange`.

    :type  innerGridLimit: :class:`dict`
    :param innerGridLimit: an optional domain limit that can be used to cull
                           tropical cyclone paths. The :class:`dict` should
                           contain the keys :attr:`xMin`, :attr:`xMax`,
                           :attr:`yMin` and :attr:`yMax`. The *x* variable
                           bounds the latitude and the *y* variable bounds the
                           longitude.

    :type  dt: float
    :param dt: the time step used the the track simulation.

    :type  maxTimeSteps: int
    :param maxTimeSteps: the maximum number of tropical cyclone time steps that
                         will be simulated.

    :type  sizeMean: float (default: 57.0)
    :param sizeMean: the fallback average tropical cyclone size to use when the
                     empirical distribution data cannot be loaded from file.
                     The default value is taken from the paper::

                     McConochie, J.D., T.A. Hardy and L.B. Mason (2004). Modelling
                     tropical cyclone over-water wind and pressure fields.  Ocean
                     Engineering, 31, 1757-1782

    :type  sizeStdDev: float (default: 0.6)
    :param sizeStdDev: the fallback standard deviation of the tropical cyclone
                       size to use when the empirical distribution data cannot
                       be loaded from file. The default value is taken from the
                       paper::

                       McConochie, J.D., T.A. Hardy and L.B. Mason (2004). Modelling
                       tropical cyclone over-water wind and pressure fields.  Ocean
                       Engineering, 31, 1757-1782

    """

    def __init__(self, processPath, gridLimit, gridSpace, gridInc, mslp,
                 landfall, innerGridLimit=None, dt=1.0, maxTimeSteps=360,
                 sizeMean=57.0, sizeStdDev=0.6):

        self.processPath        = processPath
        self.gridLimit          = gridLimit
        self.gridSpace          = gridSpace
        self.gridInc            = gridInc
        self.mslp               = mslp
        self.landfall           = landfall
        self.innerGridLimit     = innerGridLimit
        self.dt                 = dt
        self.maxTimeSteps       = maxTimeSteps
        self.sizeMean           = sizeMean
        self.sizeStdDev         = sizeStdDev
        self.timeOverflow       = dt * maxTimeSteps
        self.missingValue       = sys.maxint  # FIXME: remove
        self.progressbar        = None  # FIXME: remove
        self.allCDFInitBearing  = None
        self.allCDFInitSpeed    = None
        self.allCDFInitPressure = None
        self.allCDFInitSize     = None
        self.cdfSize            = None
        self.vStats             = None
        self.pStats             = None
        self.bStats             = None
        self.dpStats            = None

        originDistFile = pjoin(processPath, 'originPDF.nc')
        self.originSampler = SamplingOrigin(originDistFile, None, None)

    def loadInitialConditionDistributions(self):
        """
        Load the tropical cyclone empirical distribution data for initial bearing,
        initial speed, and initial pressure. The method will try to load the files

            all_cell_cdf_init_bearing.nc
            all_cell_cdf_init_speed.nc
            all_cell_cdf_init_pressure.nc
        
        from the :attr:`processPath` directory. If it can't find those files
        then it will fallback to the csv versions of those files. Note: the csv
        file names do *not* end with `.csv`.

        The empirical distribution data will be used to initialise tropical cyclone
        tracks with random values when :attr:`initBearing`,  :attr:`initSpeed`,
        and :attr:`initPressure` are not provided to :meth:`generateTracks`.
        """

        def load(filename):
            """
            Helper function that loads the data from a file.
            """

            # try to load the netcdf version of the file

            if os.path.isfile(filename + '.nc'):
                log.debug('Loading data from %s.nc' % filename)
                ncdf = netcdf_file(filename + '.nc', 'r')
                i = ncdf.variables['cell'][:]
                x = ncdf.variables['x'][:]
                y = ncdf.variables['y'][:]
                return np.vstack((i, x, y)).T

            # otherwise, revert to old csv format
            
            log.info('Could not load %s.nc, reverting to old format.' % filename)
            return flLoadFile(filename, '%', ',')

        # Load the files
        
        log.debug('Loading the cyclone parameter data')

        path = self.processPath
        try:
            self.allCDFInitBearing  = load(pjoin(path, 'all_cell_cdf_init_bearing'))
            self.allCDFInitSpeed    = load(pjoin(path, 'all_cell_cdf_init_speed'))
            self.allCDFInitPressure = load(pjoin(path, 'all_cell_cdf_init_pressure'))

        except IOError:
            log.critical('CDF distribution file %s does not exist!' % filename)
            log.critical('Run AllDistribution option in main to generate' + \
                         ' those files.')
            raise
    
        try:
            self.allCDFInitSize = load(pjoin(path, 'all_cell_cdf_init_rmax'))

        except IOError:
            log.warning('RMW distribution file does not exist! Assuming ' + \
                        'lognormal with mean %f and stdev %f.' \
                        % (self.sizeMean, self.sizeStdDev))
            self.cdfSize = np.array(stats.rMaxDist(self.sizeMean, 
                                                   self.sizeStdDev, 
                                                   maxrad=120.0)).T


    def calculateCellStatistics(self, minSample=100):
        """
        Calculate the cell statistics for speed, bearing, pressure, and
        pressure rate of change for all the grid cells in the domain. 
        
        The statistics calculated are mean, variance, and autocorrelation.
        
        The cell statistics are calculated on a grid defined by
        :attr:`gridLimit`, :attr:`gridSpace` and :attr:`gridInc` using an
        instance of :class:`StatInterface.generateStats.GenerateStats`.

        An optional :attr:`minSample` can be given which sets the minimum
        number of observations in a given cell to calculate the statistics.
        """

        def calculate(filename, angular=False):
            """
            Helper function to calculate the statistics.
            """
            return GenerateStats(
                    pjoin(self.processPath, filename),
                    pjoin(self.processPath, 'all_lon_lat'),
                    self.gridLimit, 
                    self.gridSpace,
                    self.gridInc,
                    minSample=minSample,
                    angular=angular)

        log.debug('Calculating cell statistics for speed')
        self.vStats = calculate('all_speed')

        log.debug('Calculating cell statistics for pressure')
        self.pStats = calculate('all_pressure')

        log.debug('Calculating cell statistics for bearing')
        self.bStats = calculate('all_bearing', angular=True)

        log.debug('Calculating cell statistics for pressure rate of change')
        self.dpStats = calculate('pressure_rate')


    def saveCellStatistics(self):
        """
        Save the cell statistics for speed, bearing, pressure, and pressure
        rate of change to netcdf files.

        This method can be used with :meth:`loadCellStatistics` to avoid
        calculating the cell statistics each time the track generation is
        performed.

        This method saves the statistics to the files
            
            speed_stats.nc
            pressure_stats.nc
            bearing_stats.nc
            pressure_rate_stats.nc

        in the :attr:`processPath` directory.
        """

        path = self.processPath

        log.debug('Saving cell statistics for speed to netcdf file')
        self.vStats.save(pjoin(path, 'speed_stats.nc'), 'speed')

        log.debug('Saving cell statistics for bearing to netcdf file')
        self.bStats.save(pjoin(path, 'bearing_stats.nc'), 'bearing')

        log.debug('Saving cell statistics for pressure to netcdf file')
        self.pStats.save(pjoin(path, 'pressure_stats.nc'), 'pressure')

        log.debug('Saving cell statistics for pressure rate to netcdf file')
        self.dpStats.save(pjoin(path, 'pressure_rate_stats.nc'), 'pressure rate')

    def loadCellStatistics(self):
        """
        Load the cell statistics for speed, bearing, pressure, and pressure
        rate of change from netcdf files.

        This method loads the statistics from the files
            
            speed_stats.nc
            pressure_stats.nc
            bearing_stats.nc
            pressure_rate_stats.nc

        in the :attr:`processPath` directory.
        """

        def init(filename, angular=False):
            """
            Helper function to initialise :class:`GenerateStats`.
            """
            return GenerateStats(
                    pjoin(self.processPath, filename),
                    pjoin(self.processPath, 'all_lon_lat'),
                    self.gridLimit, 
                    self.gridSpace,
                    self.gridInc,
                    angular=angular,
                    calculateLater=True)

        log.debug('Loading cell statistics for speed from netcdf file')
        self.vStats = init('all_speed')
        self.vStats.load(pjoin(self.processPath, 'speed_stats.nc'))

        log.debug('Loading cell statistics for pressure from netcdf file')
        self.pStats = init('all_pressure')
        self.pStats.load(pjoin(self.processPath, 'pressure_stats.nc'))

        log.debug('Loading cell statistics for bearing from netcdf file')
        self.bStats = init('all_bearing', angular=True)
        self.bStats.load(pjoin(self.processPath, 'bearing_stats.nc'))

        log.debug('Loading cell statistics for pressure_rate from netcdf file')
        self.dpStats = init('pressure_rate')
        self.dpStats.load(pjoin(self.processPath, 'pressure_rate_stats.nc'))

    def generateTracks(self, nTracks, initLon=None, initLat=None, initSpeed=None, 
                       initBearing=None, initPressure=None, initEnvPressure=None, 
                       initRmax=None):
        """
        Generate tropical cyclone tracks from a single genesis point.

        If the initial conditions for speed, pressure, and bearing are not
        provided then they will be drawn randomly from the empirical distributions
        that were calculated from historical data (see
        :meth:`loadInitialConditionDistributions`).

        If the genesis point (initLon, initLat) is not provided, then an origin
        will be randomly chosen using the empirical genesis point distribution
        calculated by :class:`StatInterface.SamplingOrigin`. However, if this
        random point (or the initial point given) falls outside the domain
        defined by :attr:`gridLimit` then no tracks will be
        generated.

        :type  nTracks: int
        :param nTracks: the number of tracks to generate from the genesis
                        point.

        :type  initLon: float
        :param initLon: the longitude of the genesis point.

        :type  initLat: float
        :param initLat: the latitude of the genesis point.

        :type  initSpeed: float
        :param initSpeed: the initial speed of the tropical cyclone.

        :type  initBearing: float
        :param initBearing: the initial bearing of the tropical cyclone.

        :type  initPressure: float
        :param initPressure: the initial pressure of the tropical cyclone.

        :type  initEnvPressure: float
        :param initEnvPressure: the initial environment pressure.

        :type  initRmax: float
        :param initRmax: the initial maximum radius of the tropical cyclone.
        

        :rtype :class:`numpy.array`
        :return: the tracks generated.
        """

        log.debug('Generating %d tropical cyclone tracks' % nTracks)

        results = []

        if not (initLon and initLat):
            log.debug('Cyclone origin not given, sampling a random one instead.')
            initLon, initLat = self.originSampler.ppf(uniform(), uniform())

        # Get the initial grid cell

        initCellNum = Cstats.getCellNum(initLon, initLat, 
                                        self.gridLimit, self.gridSpace)

        log.debug("Cyclones origin: (%6.2f, %6.2f) Cell: %i Grid: %s" % \
                  (initLon, initLat, initCellNum, self.gridLimit))

        # Sample an initial bearing if none is provided

        if not initBearing:
            ind = self.allCDFInitBearing[:, 0] == initCellNum
            cdfInitBearing = self.allCDFInitBearing[ind, 1:3]
            initBearing = ppf(uniform(), cdfInitBearing)

        # Sample an initial speed if none is provided
        
        if not initSpeed:
            ind = self.allCDFInitSpeed[:, 0] == initCellNum
            cdfInitSpeed = self.allCDFInitSpeed[ind, 1:3]
            initSpeed = ppf(uniform(), cdfInitSpeed)

        # Sample an initial environment pressure if none is provided

        if not initEnvPressure:
            initEnvPressure = self.mslp.sampleGrid(initLon, initLat)

        # Sample an initial pressure if none is provided

        if not initPressure:
            # Sample subject to the constraint initPressure < initEnvPressure
            ind = self.allCDFInitPressure[:, 0] == initCellNum
            cdfInitPressure = self.allCDFInitPressure[ind, 1:3]
            ix = cdfInitPressure[:, 0].searchsorted(initEnvPressure)
            upperProb = cdfInitPressure[ix-1, 1]
            initPressure = ppf(uniform(0.0, upperProb), cdfInitPressure)

        # Sample an initial maximum radius if none is provided
        
        if not initRmax:
            if not self.allCDFInitSize:
                cdfSize = self.cdfSize[:, [0, 2]]
            else:
                ind = self.allCDFInitSize[:, 0] == initCellNum
                cdfSize = self.allCDFInitSize[ind, 1:3]
            initRmax = ppf(uniform(), cdfSize)


        # Do not generate tracks from this genesis point if we are going to
        # exit the domain on the first step
        
        nextLon, nextLat = maputils.bear2LatLon(initBearing, self.dt*initSpeed, initLon, initLat)

        log.debug('initBearing: %.2f initSpeed: %.2f initEnvPressure: %.2f initPressure: %.2f' % (initBearing, initSpeed, initEnvPressure, initPressure))
        log.debug('Next step: (%.2f, %.2f) to (%.2f, %.2f)' % (initLon, initLat, nextLon, nextLat))

        if not ((self.gridLimit['xMin'] <= nextLon <= self.gridLimit['xMax'])
            and (self.gridLimit['yMin'] <= nextLat <= self.gridLimit['yMax'])):
            log.debug('Tracks will exit domain immediately for this genesis point.')
            return np.array(results).T

        # Generate a `nTracks` tracks from the genesis point

        for j in range(1, nTracks+1):
            log.debug('** Generating track %i from point (%.2f,%.2f)' \
                      % (j, initLon, initLat))
            track = self._singleTrack(j, initLon, initLat, initSpeed, initBearing, 
                                      initPressure, initEnvPressure, initRmax)
            results.append(track)
   
        # Define some filter functions

        def empty(track):
            """
            :return: True if the track is empty. False, otherwise.
            """
            index, age, lon, lat, speed, bearing, pressure, penv, rmax = track
            return len(lon) == 0

        def died_early(track, minAge=12):
            """
            :return: True if the track dies before `minAge`. False, otherwise.
            """
            index, age, lon, lat, speed, bearing, pressure, penv, rmax = track
            return age[-1] < minAge

        def inside_domain(track):
            """
            :return: True if the track stays inside the domain. False, otherwise.
            """
            index, age, lon, lat, speed, bearing, pressure, penv, rmax = track
            inside = [lon[k] > self.innerGridLimit['xMin'] and
                      lon[k] < self.innerGridLimit['xMax'] and
                      lat[k] > self.innerGridLimit['yMin'] and
                      lat[k] < self.innerGridLimit['yMax']
                      for k in range(len(lon))]
            return all(inside)

        def valid_pressures(track):
            index, age, lon, lat, speed, bearing, pressure, penv, rmax = track
            return all(pressure < penv)

        # Filter the generated tracks based on certain criteria

        nbefore = len(results)
        results = [track for track in results if not empty(track)]
        log.debug('Removed %i empty tracks.' % (nbefore - len(results)))

        nbefore = len(results)
        results = [track for track in results if not died_early(track)]
        log.debug('Removed %i tracks that died early.' %
                  (nbefore - len(results)))

        nbefore = len(results)
        results = [track for track in results if valid_pressures(track)]
        log.debug('Removed %i tracks that had incorrect pressures.' %
                  (nbefore - len(results)))

        if self.innerGridLimit:
            nbefore = len(results)
            results = [track for track in results if inside_domain(track)]
            log.debug('Removed %i tracks that do not pass inside domain.' %
                     (nbefore  - len(results)))

        # Return the tracks as an stacked array

        if len(results) > 1:
            return np.hstack([np.vstack(r) for r in results]).T
        else:
            return np.array(results).T

    def generateTracksToFile(self, outputFile, nTracks, initLon=None,
                           initLat=None, initSpeed=None, initBearing=None,
                           initPressure=None, initEnvPressure=None, initRmax=None):
        """
        Generate tropical cyclone tracks from a single genesis point and save
        the tracks to a file. 
        
        This is a helper function that calls :meth:`generateTracks`.

        :type  outputFile: str
        :param outputFile: the filename of the file where the tracks will be
                           saved. If `outputFile` has the `shp` extension then
                           it will be saved to a shp file. Otherwise, the
                           tracks will be saved in csv format.
        """

        results = self.generateTracks(nTracks, initLon=initLon, initLat=initLat, 
                                      initSpeed=initSpeed, initBearing=initBearing, 
                                      initPressure=initPressure, 
                                      initEnvPressure=initEnvPressure,
                                      initRmax=initRmax)

        if outputFile.endswith("shp"):
            log.debug('Outputting data into %s' % outputFile)

            fields = {}
            fields['Index'] = {
                'Type': 1, 'Length': 5, 'Precision': 0, 'Data': results[:, 0]}
            fields['Time'] = {
                'Type': 2, 'Length': 7, 'Precision': 1, 'Data': results[:, 1]}
            fields['Longitude'] = {
                'Type': 2, 'Length': 7, 'Precision': 2, 'Data': results[:, 2]}
            fields['Latitude'] = {
                'Type': 2, 'Length': 7, 'Precision': 2, 'Data': results[:, 3]}
            fields['Speed'] = {
                'Type': 2, 'Length': 6, 'Precision': 1, 'Data': results[:, 4]}
            fields['Bearing'] = {
                'Type': 2, 'Length': 6, 'Precision': 1, 'Data': results[:, 5]}
            fields['Pressure'] = {
                'Type': 2, 'Length': 6, 'Precision': 1, 'Data': results[:, 6]}
            fields['pEnv'] = {
                'Type': 2, 'Length': 6, 'Precision': 1, 'Data': results[:, 7]}
            fields['rMax'] = {
                'Type': 2, 'Length': 5, 'Precision': 1, 'Data': results[:, 8]}

            args = {'filename': outputFile, 'lon': results[:, 2], 
                    'lat': results[:, 3], 'fields': fields}

            thr = AsyncRun(shpSaveTrackFile, args)
            try:
                thr.start()
            except:
                raise
        else:
            log.debug('Outputting data into %s' % outputFile)

            header = 'CycloneNumber,TimeElapsed(hr),Longitude(degree)' \
                   + ',Latitude(degree),Speed(km/hr),Bearing(degrees)' \
                   + ',CentralPressure(hPa),EnvPressure(hPa),rMax(km)'
            args = {"filename": outputFile, "data": results, "header": header,
                    "delimiter": ',', "fmt": '%7.2f'}

            fl = AsyncRun(flSaveFile, args)
            fl.start()

    def _singleTrack(self, cycloneNumber, initLon, initLat, initSpeed,
                     initBearing, initPressure, initEnvPressure, initRmax):
        """
        Generate a single tropical cyclone track from a genesis point.

        :type  cycloneNumber: int
        :param cycloneNumer: the tropical cyclone index.

        :type  initLon: float
        :param initLon: the longitude of the genesis point.

        :type  initLat: float
        :param initLat: the latitude of the genesis point.

        :type  initSpeed: float
        :param initSpeed: the initial speed of the tropical cyclone.

        :type  initBearing: float
        :param initBearing: the initial bearing of the tropical cyclone.

        :type  initPressure: float
        :param initPressure: the initial pressure of the tropical cyclone.

        :type  initEnvPressure: float
        :param initEnvPressure: the initial environment pressure.

        :type  initRmax: float
        :param initRmax: the initial maximum radius of the tropical cyclone.


        :return: a tuple of :class:`numpy.ndarray`'s 
                 The tuple consists of::

                      index - the tropical cyclone index
                      age - age of the tropical cyclone
                      lon - longitude
                      lat - latitude
                      speed
                      bearing
                      pressure
                      penv - environment pressure
                      rmax - maximum radius
        """

        index    = np.ones(self.maxTimeSteps, 'f') * cycloneNumber
        age      = np.empty(self.maxTimeSteps, 'i')
        lon      = np.empty(self.maxTimeSteps, 'f')
        lat      = np.empty(self.maxTimeSteps, 'f')
        speed    = np.empty(self.maxTimeSteps, 'f')
        bearing  = np.empty(self.maxTimeSteps, 'f')
        pressure = np.empty(self.maxTimeSteps, 'f')
        penv     = np.empty(self.maxTimeSteps, 'f')
        rmax     = np.empty(self.maxTimeSteps, 'f')
        land     = np.empty(self.maxTimeSteps, 'i')
        dist     = np.empty(self.maxTimeSteps, 'f')

        # Initialise the track
        
        age[0]      = 0
        lon[0]      = initLon
        lat[0]      = initLat
        speed[0]    = initSpeed
        bearing[0]  = initBearing
        pressure[0] = initPressure
        penv[0]     = initEnvPressure
        rmax[0]     = initRmax
        land[0]     = 0
        dist[0]     = self.dt * initSpeed

        # Initialise variables that will be used when performing a step

        self.offshorePressure = initPressure
        self.theta = initBearing
        self.v     = initSpeed
        self.vChi  = 0.0
        self.bChi  = 0.0
        self.pChi  = 0.0
        self.sChi  = 0.0
        self.dpChi = 0.0
        self.dsChi = 0.0
        self.dp    = 0.0
        self.ds    = 0.0

        # Initialise the landfall tolerance
        
        tolerance = 0.0

        # Generate the track

        for i in xrange(1, self.maxTimeSteps):

            # Get the new latitude and longitude from bearing and distance
            
            lon[i], lat[i] = Cmap.bear2LatLon(bearing[i-1], dist[i-1], 
                                              lon[i-1], lat[i-1])

            # Sample the environment pressure
            
            penv[i] = self.mslp.sampleGrid(lon[i], lat[i])

            # Terminate and return the track if it steps out of the domain

            if (lon[i] <  self.gridLimit['xMin'] or
                lon[i] >= self.gridLimit['xMax'] or
                lat[i] <= self.gridLimit['yMin'] or
                lat[i] >  self.gridLimit['yMax']):

                log.debug('TC stepped out of grid at point ' + \
                          '(%.2f %.2f) and time %i' % (lon[i], lat[i], i))

                return index[:i], age[:i], lon[:i], lat[:i], speed[:i], \
                       bearing[:i], pressure[:i], penv[:i], rmax[:i]

            cellNum = Cstats.getCellNum(lon[i], lat[i], 
                                        self.gridLimit, self.gridSpace)
            on_land = self.landfall.onLand(lon[i], lat[i])

            land[i] = on_land

            # Do the real work: generate a step of the model
            
            self._stepPressureChange(cellNum, i, on_land)
            self._stepBearing(cellNum, i, on_land)
            self._stepSpeed(cellNum, i, on_land)

            # Update bearing, speed and pressure

            bearing[i] = self.theta
            speed[i] = abs(self.v)  # reflect negative speeds
            pressure[i] = pressure[i-1] + self.dp*self.dt

            # Calculate the central pressure
            
            if on_land:
                tolerance += float(self.dt)
                deltaP = self.offshorePressure - penv[i]
                alpha = 0.008 + 0.0008*deltaP + normal(0, 0.001)
                pressure[i] = penv[i] - deltaP*np.exp(-alpha*tolerance)
                log.debug("Central pressure: %7.2f" % pressure[i])
            else:
                pstat = self.pStats.coeffs
                pressure[i] = pressure[i-1] + self.dp*self.dt

                # If the central pressure of the synthetic storm is more than 4
                # std deviations lower than the minimum observed central
                # pressure, automatically start raising the central pressure.
                
                if (pressure[i] < (pstat.min[cellNum]-4.*pstat.sig[cellNum])):
                    log.debug("Pressure is extremely low - recalculating")
                    pressure[i] = pressure[i-1] + abs(self.dp)*self.dt

                self.offshorePressure = pressure[i]

            # If the empirical distribution of tropical cyclone size is
            # loaded then sample and update the maximum radius. Otherwise, 
            # keep the maximum radius constant.

            if self.allCDFInitSize:
                self._stepSizeChange(cellNum, i, on_land)
                rmax[i] = rmax[i-1] + self.ds*self.dt
                # if the radius goes below 1.0, then do an 
                # antithetic increment instead
                if rmax[i] <= 1.0:
                    rmax[i] = rmax[i-1] - self.ds*self.dt
            else:
                rmax[i] = rmax[i-1]

            # Update the distance and the age of the cyclone

            dist[i] = self.dt * speed[i]
            age[i] = age[i-1] + self.dt

            # Terminate the track if it doesn't satisfy certain criteria
            
            if self._notValidTrackStep(pressure[i], penv[i], age[i], 
                                       lon[0], lat[0], lon[i], lat[i]):
               log.debug('Track no longer satisfies criteria, terminating' + \
                         ' at time %i.' % i)
               return index[:i], age[:i], lon[:i], lat[:i], \
                      speed[:i], bearing[:i], pressure[:i], penv[:i], rmax[:i]

        return index, age, lon, lat, speed, bearing, pressure, penv, rmax

    def _stepPressureChange(self, c, i, onLand):
        """
        Take one step of the pressure change model.

        This updates :attr:`self.dpChi` and :attr:`self.dp` based on an
        (inhomogeneous) AR(1) model.

        :type  c: int
        :param c: a valid cell index in the domain

        :type  i: int
        :param i: the step number (i.e., time)

        :type  onLand: bool
        :param onLand: True if the tropical cyclone is currently over land.
        
        """
        
        # Change the parameter set accordingly
        
        if onLand:
            alpha_dp = self.dpStats.coeffs.lalpha
            phi_dp = self.dpStats.coeffs.lphi
            mu_dp = self.dpStats.coeffs.lmu
            sigma_dp = self.dpStats.coeffs.lsig
        else:
            alpha_dp = self.dpStats.coeffs.alpha
            phi_dp = self.dpStats.coeffs.phi
            mu_dp = self.dpStats.coeffs.mu
            sigma_dp = self.dpStats.coeffs.sig

        # Do the step

        self.dpChi = alpha_dp[c]*self.dpChi + phi_dp[c]*normal()

        if i == 1:
            self.dp += sigma_dp[c]*self.dpChi
        else:
            self.dp = mu_dp[c] + sigma_dp[c]*self.dpChi

    def _stepBearing(self, c, i, onLand):
        """
        Take one step of the bearing model.

        This updates :attr:`self.bChi` and :attr:`self.theta` based on an
        (inhomogeneous) AR(1) model.

        :type  c: int
        :param c: a valid cell index in the domain

        :type  t: int
        :param t: the step number (i.e., time)

        :type  onLand: bool
        :param onLand: True if the tropical cyclone is currently over land.
        
        """
        
        # Change the parameter set accordingly
        
        if onLand:
            alpha_b = self.bStats.coeffs.lalpha
            phi_b = self.bStats.coeffs.lphi
            mu_b = self.bStats.coeffs.lmu
            sigma_b = self.bStats.coeffs.lsig
        else:
            alpha_b = self.bStats.coeffs.alpha
            phi_b = self.bStats.coeffs.phi
            mu_b = self.bStats.coeffs.mu
            sigma_b = self.bStats.coeffs.sig

        # Do the step

        self.bChi = alpha_b[c]*self.bChi + phi_b[c]*normal()

        # Update the bearing

        if i == 1:
            self.theta += math.degrees(sigma_b[c]*self.bChi)
        else:
            self.theta = math.degrees(mu_b[c] + sigma_b[c]*self.bChi)

        self.theta = np.mod(self.theta, 360.)


    def _stepSpeed(self, c, i, onLand):
        """
        Take one step of the speed model.

        This updates :attr:`self.vChi` and :attr:`self.v` based on an
        (inhomogeneous) AR(1) model.

        :type  c: int
        :param c: a valid cell index in the domain

        :type  t: int
        :param t: the step number (i.e., time)

        :type  onLand: bool
        :param onLand: True if the tropical cyclone is currently over land.
        
        """
        
        # Change the parameter set accordingly
        
        if onLand:
            alpha_v = self.vStats.coeffs.lalpha
            phi_v = self.vStats.coeffs.lphi
            mu_v = self.vStats.coeffs.lmu
            sigma_v = self.vStats.coeffs.lsig
        else:
            alpha_v = self.vStats.coeffs.alpha
            phi_v = self.vStats.coeffs.phi
            mu_v = self.vStats.coeffs.mu
            sigma_v = self.vStats.coeffs.sig

        # Do the step

        self.vChi = alpha_v[c]*self.vChi + phi_v[c]*normal()

        # Update the speed

        if i == 1:
            self.v += abs(sigma_v[c]*self.vChi)
        else:
            self.v = abs(mu_v[c] + sigma_v[c]*self.vChi)

    def _stepSizeChange(self, c, i, onLand):
        """
        Take one step of the size change model.

        This updates :attr:`self.vChi` and :attr:`self.v` based on an
        (inhomogeneous) AR(1) model.

        :type  c: int
        :param c: a valid cell index in the domain

        :type  t: int
        :param t: the step number (i.e., time)

        :type  onLand: bool
        :param onLand: True if the tropical cyclone is currently over land.
        
        """
        
        # Change the parameter set accordingly
        
        if onLand:
            alpha_ds = self.dsStats.coeffs.lalpha
            phi_ds = self.dsStats.coeffs.lphi
            mu_ds = self.dsStats.coeffs.lmu
            sigma_ds = self.dsStats.coeffs.lsig
        else:
            alpha_ds = self.dsStats.coeffs.alpha
            phi_ds = self.dsStats.coeffs.phi
            mu_ds = self.dsStats.coeffs.mu
            sigma_ds = self.dsStats.coeffs.sig

        # Do the step

        self.dsChi = alpha_ds[c]*self.dsChi + phi_ds[c]*normal()

        # Update the size change

        if i == 1:
            self.ds += sigma_ds[c]*self.dsChi
        else:
            self.ds = mu_ds[c] + sigma_ds[c]*self.dsChi

    def _notValidTrackStep(self, pressure, penv, age, lon0, lat0, nextlon, nextlat):
        """
        This is called to check if a tropical cyclone track meets certain
        conditions.
        """

        if age > 12 and (abs(penv - pressure) < 5.0):
            log.debug('Pressure difference < 5.0 (penv: %f pressure: %f)' % \
                    (penv, pressure))
            return True

        return False

    def dumpAllCellCoefficients(self):
        """
        Dump all cell coefficients to a netcdf file to permit further analysis.

        """
        lon = arange(self.gridLimit['xMin'], self.gridLimit[
                     'xMax'], self.gridSpace['x'])
        lat = arange(self.gridLimit['yMax'], self.gridLimit[
                     'yMin'], -1*self.gridSpace['y'])

        nx = len(lon)
        ny = len(lat)

        dimensions = {0: {'name': 'lat', 'values': lat, 'dtype': 'f',
                          'atts': {'long_name': 'Latitude', 'units': 'degrees_north'}},
                      1: {'name': 'lon', 'values': lon, 'dtype': 'f',
                          'atts': {'long_name': 'Longitude', 'units': 'degrees_east'}}}

        variables = {0: {'name': 'vmu', 'dims': ('lat', 'lon'),
                         'values': self.vStats.coeffs.mu.reshape((ny, nx)),
                         'dtype': 'f',
                         'atts': {'long_name': 'Mean forward speed',
                                  'units': 'm/s'}},
                     1: {'name': 'valpha', 'dims': ('lat', 'lon'),
                        'values': self.vStats.coeffs.alpha.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Lag-1 autocorrelation of forward speed',
                                'units': ''}},
                     2: {'name': 'vsig', 'dims': ('lat', 'lon'),
                        'values': self.vStats.coeffs.sig.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Standard deviation forward speed',
                                'units': 'm/s'}},
                     3: {'name': 'vmin', 'dims': ('lat', 'lon'),
                        'values': self.vStats.coeffs.min.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Minimum forward speed',
                                'units': 'm/s'}},
                     4: {'name': 'vlmu', 'dims': ('lat', 'lon'),
                        'values': self.vStats.coeffs.lmu.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Mean forward speed (over land)',
                                'units': 'm/s'}},
                     5: {'name': 'vlalpha', 'dims': ('lat', 'lon'),
                        'values': self.vStats.coeffs.lalpha.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Lag-1 autocorrelation of forward speed (over land)',
                                'units': ''}},
                     6: {'name': 'vlsig', 'dims': ('lat', 'lon'),
                        'values': self.vStats.coeffs.lsig.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Standard deviation of forward speed (over land)',
                                'units': 'm/s'}},
                     7: {'name': 'vlmin', 'dims': ('lat', 'lon'),
                        'values': self.vStats.coeffs.lmin.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Minimum forward speed (over land)',
                                'units': 'm/s'}},

                     8: {'name': 'bmu', 'dims': ('lat', 'lon'),
                        'values': self.bStats.coeffs.mu.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Mean bearing',
                                'units': 'degrees'}},
                     9: {'name': 'balpha', 'dims': ('lat', 'lon'),
                        'values': self.bStats.coeffs.alpha.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Lag-1 autocorrelation of bearing',
                                'units': ''}},
                     10: {'name': 'bsig', 'dims': ('lat', 'lon'),
                        'values': self.bStats.coeffs.sig.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Standard deviation of bearing',
                                'units': 'degrees'}},
                     11: {'name': 'bmin', 'dims': ('lat', 'lon'),
                        'values': self.bStats.coeffs.min.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Minimum bearing',
                                'units': 'degrees'}},
                     12: {'name': 'blmu', 'dims': ('lat', 'lon'),
                        'values': self.bStats.coeffs.lmu.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Mean bearing(over land)',
                                'units': 'degrees'}},
                     13: {'name': 'blalpha', 'dims': ('lat', 'lon'),
                        'values': self.bStats.coeffs.lalpha.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Lag-1 autocorrelation of bearing (over land)',
                                'units': ''}},
                     14: {'name': 'blsig', 'dims': ('lat', 'lon'),
                        'values': self.bStats.coeffs.lsig.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Standard deviation of bearing (over land)',
                                'units': 'degrees'}},
                     15: {'name': 'blmin', 'dims': ('lat', 'lon'),
                        'values': self.bStats.coeffs.lmin.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Minimum bearing (over land)',
                                'units': 'degrees'}},

                     16: {'name': 'pmu', 'dims': ('lat', 'lon'),
                        'values': self.pStats.coeffs.mu.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Mean central pressure',
                                'units': 'hPa'}},
                     17: {'name': 'palpha', 'dims': ('lat', 'lon'),
                        'values': self.pStats.coeffs.alpha.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Lag-1 autocorrelation of central pressure',
                                'units': ''}},
                     18: {'name': 'psig', 'dims': ('lat', 'lon'),
                        'values': self.pStats.coeffs.sig.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Standard deviation of central pressure',
                                'units': 'hPa'}},
                     19: {'name': 'pmin', 'dims': ('lat', 'lon'),
                        'values': self.pStats.coeffs.min.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Minimum central pressure',
                                'units': 'hPa'}},
                     20: {'name': 'plmu', 'dims': ('lat', 'lon'),
                        'values': self.pStats.coeffs.lmu.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Mean central pressure (over land)',
                                'units': 'hPa'}},
                     21: {'name': 'plalpha', 'dims': ('lat', 'lon'),
                        'values': self.pStats.coeffs.lalpha.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Lag-1 autocorrelation of central pressure (over land)',
                                'units': ''}},
                     22: {'name': 'plsig', 'dims': ('lat', 'lon'),
                        'values': self.pStats.coeffs.lsig.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Standard deviation of central pressure (over land)',
                                'units': 'hPa'}},
                     23: {'name': 'plmin', 'dims': ('lat', 'lon'),
                        'values': self.pStats.coeffs.lmin.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Minimum central pressure (over land)',
                                'units': 'hPa'}},

                     24: {'name': 'dpmu', 'dims': ('lat', 'lon'),
                        'values': self.dpStats.coeffs.mu.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Mean rate of pressure change',
                                'units': 'hPa/h'}},
                     25: {'name': 'dpalpha', 'dims': ('lat', 'lon'),
                        'values': self.dpStats.coeffs.alpha.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Lag-1 autocorrelation of rate of pressure change',
                                'units': ''}},
                     26: {'name': 'dpsig', 'dims': ('lat', 'lon'),
                        'values': self.dpStats.coeffs.sig.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Standard deviation of rate of pressure change',
                                'units': 'hPa/h'}},
                     27: {'name': 'dpmin', 'dims': ('lat', 'lon'),
                        'values': self.dpStats.coeffs.min.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Minimum rate of pressure change',
                                'units': 'hPa/h'}},
                     28: {'name': 'dplmu', 'dims': ('lat', 'lon'),
                        'values': self.dpStats.coeffs.lmu.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Mean rate of pressure change (over land)',
                                'units': 'hPa/h'}},
                     29: {'name': 'dplalpha', 'dims': ('lat', 'lon'),
                        'values': self.dpStats.coeffs.lalpha.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Lag-1 autocorrelation of rate of pressure change (over land)',
                                'units': ''}},
                     30: {'name': 'dplsig', 'dims': ('lat', 'lon'),
                        'values': self.dpStats.coeffs.lsig.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Standard deviation of rate of pressure change (over land)',
                                'units': 'hPa/h'}},
                     31: {'name': 'dplmin', 'dims': ('lat', 'lon'),
                        'values': self.dpStats.coeffs.lmin.reshape((ny, nx)),
                        'dtype': 'f',
                        'atts': {'long_name': 'Minimum rate of pressure change (over land)',
                                'units': 'hPa/h'}}}

        outputFile = pjoin(self.processPath, 'coefficients.nc')
        nctools.ncSaveGrid(outputFile, dimensions, variables,
                           nodata=self.missingValue, datatitle=None, dtype='f',
                           writedata=True, keepfileopen=False)

    def calculateOrLoadCellStatistics(self):
        """
        Helper function to calculate the cell statistics if they have
        not been previously calculated.
        """
        exists = os.path.exists
        if not exists(pjoin(self.processPath, 'speed_stats.nc')) \
        or not exists(pjoin(self.processPath, 'pressure_stats.nc')) \
        or not exists(pjoin(self.processPath, 'bearing_stats.nc')) \
        or not exists(pjoin(self.processPath, 'pressure_rate_stats.nc')):
            self.calculateCellStatistics()
            self.saveCellStatistics()
        self.loadCellStatistics()

def attemptParallel():
    """
    Attempt to load Pypar globally as `pp`. If Pypar loads successfully, then a
    call to `pypar.finalize` is registered to be called at exit of the Python
    interpreter. This is to ensure that MPI exits cleanly.

    If pypar cannot be loaded then a dummy `pp` is created.
    """
    global pp

    try:
        # load pypar for everyone

        import pypar as pp

    except ImportError:

        # no pypar, create a dummy one

        class DummyPypar(object):
            def size(self): return 1
            def rank(self): return 0
            def barrier(self): pass

        pp = DummyPypar()


# Define a global pseudo-random number generator. This is done to ensure
# we are sampling correctly across processors when performing the simulation 
# in parallel. We use the inbuilt Python `random` library as it provides the
# ability to `jumpahead` in the stream (as opposed to `numpy.random`).

PRNG = random.Random()


def normal(mean=0.0, stddev=1.0):
    """
    Sample from a Normal distribution.
    """
    return PRNG.normalvariate(mean, stddev)


def uniform(a=0.0, b=1.0):
    """
    Sample from a uniform distribution.
    """
    return PRNG.uniform(a,b)


def ppf(q, cdf):
    """
    Percentage point function (aka. inverse CDF, quantile) of
    an empirical CDF.

    This is used to sample from an empirical distribution.
    """
    i = cdf[:, 1].searchsorted(q)
    return cdf[i, 0]


def balanced(iterable):
    """
    Balance an iterator across processors.

    This partitions the work evenly across processors. However, it requires
    the iterator to have been generated on all processors before hand. This is
    only some magical slicing of the iterator, i.e., a poor man version of 
    scattering.
    """
    P, p = pp.size(), pp.rank()
    return itertools.islice(iterable, p, None, P)


class Simulation(object):
    """
    Simulation parameters.

    This is used to set the PRNG state before `ntracks` are simulated.

    :type  index: int
    :param index: the simulation index number.

    :type  seed: int
    :param seed: the initial seed used for the PRNG.

    :type  jumpahead: int
    :param jumpahead: the amount to jump ahead from the initial seed in the
                      PRNG stream.

    :type  ntracks: int
    :param ntracks: the number of tracks to be generated during the simulation.

    :type  outfile: str
    :param outfile: the filename where the tracks will be saved to.
    """

    def __init__(self, index, seed, jumpahead, ntracks, outfile):
        self.index = index
        self.seed = seed
        self.jumpahead = jumpahead
        self.ntracks = ntracks
        self.outfile = outfile


def run(configFile):
    """
    Run the tropical cyclone track generation.

    This will attempt to perform the simulation in parallel but also provides a
    sane fallback mechanism.

    :type  configFile: str
    :param configFile: the filename of the configuration file to load the
                       track generation configuration from.
    """

    log.info('Loading track generation settings')

    # Get configuration

    config = ConfigParser()
    config.read(configFile)

    outputPath     = config.get('Output', 'Path')
    nGenesisPoints = config.getint('TrackGenerator', 'NumSimulations')
    yrsPerSim      = config.getint('TrackGenerator', 'YearsPerSimulation')
    maxTimeSteps   = config.getint('TrackGenerator', 'NumTimeSteps')
    dt             = config.getfloat('TrackGenerator', 'TimeStep')
    fmt            = config.get('TrackGenerator', 'Format')
    gridSpace      = config.geteval('TrackGenerator', 'GridSpace')
    gridInc        = config.geteval('TrackGenerator', 'GridInc')
    gridLimit      = config.geteval('Region', 'gridLimit')
    mslpGrid       = config.get('Input', 'MSLPGrid')
    genesisSeed    = None
    trackSeed      = None
    trackPath      = pjoin(outputPath, 'tracks')
    processPath    = pjoin(outputPath, 'process')
    trackFilename  = 'tracks.%04i.' + fmt

    if config.has_option('TrackGenerator', 'gridLimit'):
        gridLimit = config.geteval('TrackGenerator', 'gridLimit')

    if config.has_option('TrackGenerator', 'Frequency'):
        meanFreq = config.getfloat('TrackGenerator', 'Frequency')
    else:
        log.info('No genesis frequency specified: auto-calculating')
        CalcF = CalcFrequency(configFile, gridLimit)
        meanFreq = CalcF.calc()
        log.info("Estimated annual genesis frequency for domain: %s" %
                 meanFreq)

    if config.has_option('TrackGenerator', 'GenesisSeed'):
        genesisSeed = config.getint('TrackGenerator', 'GenesisSeed')

    if config.has_option('TrackGenerator', 'TrackSeed'):
        trackSeed = config.getint('TrackGenerator', 'TrackSeed')

    # Attempt to start the track generator in parallel
    
    attemptParallel()

    if pp.size() > 1 and (not genesisSeed or not trackSeed):
        log.critical('TrackSeed and GenesisSeed are needed for parallel runs!')
        sys.exit(1)

    # Parse the MSLP setting

    mnth_sel = set(mslpGrid.strip('[]{}() ').replace(',', ' ').split(' '))
    mnth_sel.discard('')
    if mnth_sel.issubset([str(k) for k in range(1, 13)]):
        mnth_sel_int = [int(k) for k in mnth_sel]
        log.info("Generating MSLP seasonal average")
        mslp = MSLPGrid(mnth_sel_int)
    else:
        log.info("Loading MSLP seasonal average from file")
        mslp = SampleGrid(mslpGrid)

    # Initialise the landfall tracking

    landfall = trackLandfall.LandfallDecay(configFile, dt)

    # Wait for configuration to be loaded by all processors

    pp.barrier()

    # Seed the numpy PRNG. We use this PRNG to sample the number of tropical
    # cyclone tracks to simulate at each genesis point. The inbuilt Python
    # `random` library does not provide a function to sample from the Poisson
    # distribution.

    if genesisSeed:
        np.random.seed(genesisSeed)

    # Do the first stage of the simulation (i.e., sample the number of tracks to
    # simulate at each genesis point) on all processors simultaneously. Since
    # the same seed is set on all processors, they will all get exactly the
    # same simulation outcome. This also behaves correctly when not done in
    # parallel.

    nCyclones = np.random.poisson(np.floor(yrsPerSim)*meanFreq, nGenesisPoints)

    # Estimate the maximum number of random values to be drawn from the PRNG
    # for each track and calculate how much each track simulation should jump
    # ahead in the PRNG stream to ensure that it is independent of all other
    # simulations.

    maxRvsPerTrack = 4*(maxTimeSteps + 1)
    jumpAhead = np.hstack([[0], np.cumsum(nCyclones*maxRvsPerTrack)[:-1]])

    log.debug('Generating %i total tracks from %i genesis locations' %
            (sum(nCyclones), nGenesisPoints))

    # Setup the simulation parameters

    sims = []
    for i, n in enumerate(nCyclones):
        sims.append(Simulation(i, trackSeed, jumpAhead[i], n, trackFilename % i))

    # Load the track generator

    tg = TrackGenerator(processPath, gridLimit, gridSpace, gridInc, mslp,
            landfall, dt=dt, maxTimeSteps=maxTimeSteps)

    tg.loadInitialConditionDistributions()
    tg.calculateOrLoadCellStatistics()

    # Hold until all processors are ready
    
    pp.barrier()

    # Balance the simulations over the number of processors and do it

    for sim in balanced(sims):

        if sim.seed:
            PRNG.seed(sim.seed)
            PRNG.jumpahead(sim.jumpahead)
            log.debug('seed %i jumpahead %i' % (sim.seed, sim.jumpahead))

        trackFile = pjoin(trackPath, sim.outfile)
        tracks = tg.generateTracks(sim.ntracks)

        header = 'CycloneNumber,TimeElapsed(hr),Longitude(degree),Latitude(degree)' \
               + ',Speed(km/hr),Bearing(degrees),CentralPressure(hPa)' \
               + ',EnvPressure(hPa),rMax(km)'

        if len(tracks) > 0:
            np.savetxt(trackFile, tracks, header=header, comments='%', delimiter=',', 
                    fmt='%i,%10.5f,%10.5f,%10.5f,%10.5f,%10.5f,%10.5f,%10.5f,%10.5f')
        else:
            with open(trackFile, 'w') as fp:
                fp.write('%'+header)

if __name__ == "__main__":
    try:
        configFile = sys.argv[1]
    except IndexError:

        # Try loading config file with same name as python script
        configFile = __file__.rstrip('.py') + '.ini'

        # If no filename is specified and default filename doesn't exist =>
        # raise error
        if not os.path.exists(configFile):
            error_msg = "No configuration file specified, please type: python main.py {config filename}.ini"
            raise IOError, error_msg

    # If config file doesn't exist => raise error
    if not os.path.exists(configFile):
        error_msg = "Configuration file '" + configFile + "' not found"
        raise IOError, error_msg

    run(configFile)

    # Finalise MPI
    
    pp.finalize()
