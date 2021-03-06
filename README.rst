The Tropical Cyclone Risk Model
===============================

The **Tropical Cyclone Risk Model** is a computational tool developed by
`Geoscience Australia <http://www.ga.gov.au>`_ for
estimating the wind hazard from tropical cyclones. 

Due to the relatively short record of quality-controlled, consistent tropical 
cyclone observations, it is difficult to estimate average recurrence interval 
wind speeds ue to tropical cyclones. To overcome the restriction of observed 
data, TCRM uses a stochastic model to generate thousands of years of events 
that are statistically similar to the historical record. To translate these 
events to estimated wind speeds, TCRM applies a parametric windfield and 
boundary layer model to each event, Finally an extreme value distribution is 
fitted to the aggregated windfields at each grid point in the model domain to 
provide ARI wind speed estimates. 

Features
========

* **Multi-platform**: TCRM can run on desktop machines through to massively-parallel systems (tested on Windows XP/Vista/7, \*NIX);
* **Multiple options for wind field & boundary layer models**: A number of radial profiles and simple boundary layer models have been included to allow users to test sensitivity to these options.
* **Globally applicable**: Users can set up a domain in any TC basin in the globe. The model is not tuned to any one region of the globe;

Dependencies
============

* TCRM requires `Python (2.7 preferred) <https://www.python.org/>`_, `Numpy <http://www.numpy.org/>`_, `Scipy <http://www.scipy.org/>`_, `Matplotlib <http://matplotlib.org/>`_, `Basemap <http://matplotlib.org/basemap/index.html>`_, `netcdf4-python <https://code.google.com/p/netcdf4-python/>`_ and a C compiler;
* For parallel execution, `Pypar <http://github.com/daleroberts/pypar>`_ is required;

Screenshot
==========

.. image:: https://rawgithub.com/GeoscienceAustralia/tcrm/master/docs/screenshot.png

