
import os
import sys
import time
import logging as log
import argparse
import traceback

from functools import wraps
from os.path import join as pjoin, realpath, isdir, dirname, abspath

from Utilities import pathLocator
from Utilities.config import ConfigParser
from Utilities.files import flStartLog
from Utilities.progressbar import SimpleProgressBar as ProgressBar
from Evaluate import interpolateTracks

def timer(f):
    """
    Basic timing functions for entire process
    """
    @wraps(f)
    def wrap(*args, **kwargs):
        t1 = time.time()
        res = f(*args, **kwargs)
        
        tottime = time.time() - t1
        msg = "%02d:%02d:%02d " % \
          reduce(lambda ll, b : divmod(ll[0], b) + ll[1:],
                        [(tottime,), 60, 60])

        log.info("Time for %s: %s"%(f.func_name, msg) )
        return res

    return wrap

def doOutputDirectoryCreation(configFile):
    """
    Create all the necessary output folders.
    
    :param str configFile: Name of configuration file.
    :raises OSError: If the directory tree cannot be created.
    
    """
    
    config = ConfigParser()
    config.read(configFile)

    outputPath = config.get('Output', 'Path')

    log.info('Output will be stored under %s', outputPath)

    subdirs = ['tracks', 'windfield', 'plots', 'plots/timeseries',
               'log', 'process', 'process/timeseries']

    if not isdir(outputPath):
        try:
            os.makedirs(outputPath)
        except OSError:
            raise
    for subdir in subdirs:
        if not isdir(realpath(pjoin(outputPath, subdir))):
            try:
                os.makedirs(realpath(pjoin(outputPath, subdir)))
            except OSError:
                raise

def doTimeseriesPlotting(configFile):
    """
    Run functions to plot time series output
    """
    config = ConfigParser()
    config.read(configFile)

    outputPath = config.get('Output', 'Path')
    timeseriesPath = pjoin(outputPath, 'process', 'timeseries')
    plotPath = pjoin(outputPath, 'plots', 'timeseries')
    log.info("Plotting time series data to %s" % plotPath)
    from PlotInterface.plotTimeseries import plotTimeseries
    plotTimeseries(timeseriesPath, plotPath)
    
@timer
def main(configFile):

    config = ConfigParser()
    config.read(configFile)
    doOutputDirectoryCreation(configFile)
    
    trackFile = config.get('DataProcess', 'InputFile') 
    source = config.get('DataProcess', 'Source')
    delta = 1/12.
    outputPath = pjoin(config.get('Output','Path'), 'tracks')
    outputTrackFile = pjoin(outputPath, "tracks.interp.csv")

    # This will save interpolated track data in TCRM format:
    interpTrack = interpolateTracks.parseTracks(configFile, trackFile, source, delta, 
                                                outputTrackFile)
    showProgressBar = config.get('Logging', 'ProgressBar')

    pbar = ProgressBar('Calculating wind fields: ', showProgressBar)

    def status(done, total):
        pbar.update(float(done)/total)

    import wind
    wind.run(configFile, status)

    # FIXME: Add wind field and timeseries plotting
    
    doTimeseriesPlotting(configFile)
    
def startup():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config_file', 
                        help='Path to configuration file')
    parser.add_argument('-v', '--verbose', help='Verbose output', 
                        action='store_true')
    parser.add_argument('-d', '--debug', help='Allow pdb traces',
                        action='store_true')
    args = parser.parse_args()

    configFile = args.config_file
    config = ConfigParser()
    config.read(configFile)

    rootdir = pathLocator.getRootDirectory()
    os.chdir(rootdir)

    logfile = config.get('Logging','LogFile')
    logdir = dirname(realpath(logfile))

    # If log file directory does not exist, create it
    if not isdir(logdir):
        try:
            os.makedirs(logdir)
        except OSError:
            logfile = pjoin(os.getcwd(), 'tcrm.log')

    logLevel = config.get('Logging', 'LogLevel')
    verbose = config.getboolean('Logging', 'Verbose')
    debug = False

    if args.verbose:
        verbose = True

    if args.debug:
        debug = True

    flStartLog(logfile, logLevel, verbose)
    # Switch off minor warning messages
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=UserWarning, module="pytz")
    warnings.filterwarnings("ignore", category=UserWarning, module="numpy")
    warnings.filterwarnings("ignore", category=UserWarning,
                            module="matplotlib")
    
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    
    if debug:
        main(configFile)
    else:
        try:
            main(configFile)
        except Exception:  # pylint: disable=W0703
            # Catch any exceptions that occur and log them (nicely):
            tblines = traceback.format_exc().splitlines()
            for line in tblines:
                log.critical(line.lstrip())


if __name__ == "__main__":
    startup()


