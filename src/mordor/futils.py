import os
import pandas as pd
import numpy as np
import xarray as xr
import logging
import parse
from toolz import assoc_in

import mordor.utils


# logging setup
logging.basicConfig(
    filename='mordor.log',
    encoding='utf-8',
    level=logging.DEBUG,
    format='%(asctime)s %(name)s %(levelname)s:%(message)s'
)
logger = logging.getLogger(__name__)

def raw2daily(inpath, outpath, tables=None, config=None):
    config = mordor.utils.merge_config(config)

    # if None, process all available tables (from config)
    if tables is None:
        tables = [key for key in mordor.utils.read_json(config["file_logger_tables"])]

    for table in tables:
        infname = os.path.join(inpath, config["fname_raw"].format(loggername=config["logger_name"], table=table))
        days = pd.read_csv(
            infname,
            skiprows=4,
            header=None,
            usecols=[0],
            sep=',',
            dtype='S10',
        ).values
        # find unique days and first index
        udays, idays = np.unique(days, return_index=True)
        udays = pd.to_datetime(udays.astype(str))
        # add header index
        idays += 4
        # append None for slicing -> slice(idays[i],idays[i+1])
        idays = list(idays) + [None]

        # Retrieve header from original file
        with open(infname,'r') as txt:
            datalines = txt.readlines()

        N = len(datalines)

        logging.debug(f"Start write {N-4} data lines from {infname}:")
        logging.debug(f"Period {datalines[4].split(',')[0]} to {datalines[-1].split(',')[0]} ")

        # split into daily files
        for i,uday in enumerate(udays):
            # Output filename
            outfname = os.path.join(
                outpath,
                config["path_sfx"],
                config["fname_out"]
            ).format(dt=uday, datalvl='l1a',sfx='dat',table=table,**config)
            # create directory structure
            os.makedirs(os.path.dirname(outfname), exist_ok=True)
            # write daily file
            if not os.path.exists(outfname):
                with open(outfname, 'w') as txt:
                    # write header
                    txt.writelines(datalines[:4])
            # append data lines
            with open(outfname,'a') as txt:
                # write content
                islice = slice(idays[i],idays[i+1])
                txt.writelines(datalines[islice])
            logging.debug(f".. Written {len(datalines[islice])} data lines to {outfname}.")

        # remove lines from original file after writing
        with open(infname,'r') as txt:
            datalines_complete = txt.readlines()


        datalines_new = datalines_complete[:4]
        if len(datalines_complete) > N:
            datalines_new += datalines_complete[N:]

        txt = open(infname, 'w')
        txt.writelines(datalines_new)
        txt.close()
        logging.debug(f"Removed {N - 4} data lines from {infname}.")

def update_coverage_meta(ds, timevar='time'):
    """Update global attributes related to geospatial and time coverage
    """
    duration = ds[timevar].values[-1] - ds[timevar].values[0]
    resolution = np.mean(np.diff(ds[timevar].values))
    now = pd.to_datetime(np.datetime64("now"))
    gattrs = {
        'date_created': now.isoformat(),
        'geospatial_lat_min': np.nanmin(ds.lat.values),
        'geospatial_lat_max': np.nanmax(ds.lat.values),
        'geospatial_lat_units': 'degN',
        'geospatial_lon_min': np.nanmin(ds.lon.values),
        'geospatial_lon_max': np.nanmax(ds.lon.values),
        'geospatial_lon_units': 'degE',
        'time_coverage_start': pd.to_datetime(ds[timevar].values[0]).isoformat(),
        'time_coverage_end': pd.to_datetime(ds[timevar].values[-1]).isoformat(),
        'time_coverage_duration': pd.to_timedelta(duration).isoformat(),
        'time_coverage_resolution': pd.to_timedelta(resolution).isoformat(),
    }
    ds.attrs.update(gattrs)
    return ds

def stretch_resolution(ds):
    """ Stretch variable resolution to full integer size,
    to not lose resolution after averaging ADC count data."""
    for var in ds:
        if "scale_factor" not in ds[var].encoding:
            continue
        if "valid_range" not in ds[var].attrs:
            continue
        dtype = ds[var].encoding['dtype']
        valid_range = ds[var].valid_range
        int_limit = np.iinfo(dtype).max
        scale_factor = ds[var].encoding['scale_factor']
        scale_factor_mod = int((int_limit-1)/valid_range[1])
        ds[var].encoding.update({
            "scale_factor": scale_factor / scale_factor_mod,
            "_FillValue": int_limit,
        })
        ds[var].attrs.update({
            "valid_range": valid_range * scale_factor_mod
        })
    return ds

def merge_ds(ds1, ds2, timevar="time"):
    """Merge two datasets along the time dimension.
    """
    if ds1[timevar].equals(ds2[timevar]):
        logging.info("Overwrite existing file.")
        return ds2
    logging.info("Merge with existing file.")

    ## overwrite non time dependent variables
    overwrite_vars = [ v for v in ds1 if timevar not in ds1[v].dims ]

    ## merge both datasets
    ds_new=ds1.merge(ds2,
                     compat='no_conflicts',
                     overwrite_vars=overwrite_vars)

    # add global coverage attributes
    ds_new.attrs.update({'merged':1})

    # add encoding again
    ds_new = add_encoding(ds_new)
    return ds_new


def to_netcdf(ds, fname, timevar="time"):
    """xarray to netcdf, but merge if exist
    """
    # merge if necessary
    if os.path.exists(fname):
        ds1 = xr.open_dataset(fname)
        ds = merge_ds(ds1,ds, timevar=timevar)
        ds1.close()
        os.remove(fname)

    # save to netCDF4
    ds = update_coverage_meta(ds, timevar=timevar)
    ds.to_netcdf(fname,
                 encoding={timevar:{'dtype':'float64'}}) # for OpenDAP 2 compatibility

def get_cfmeta(config=None):
    """Read global and variable attributes and encoding from cfmeta.json
    """
    config= mordor.utils.merge_config(config)
    # parse the json file
    cfdict = mordor.utils.read_json(config["file_cfmeta"])
    # get global attributes:
    gattrs = cfdict['attributes']
    # apply config
    gattrs = {k:v.format_map(config) for k,v in gattrs.items()}
    # get variable attributes
    d = mordor.utils.get_var_attrs(cfdict)
    # split encoding attributes
    vattrs, vencode = mordor.utils.get_attrs_enc(d)
    return gattrs ,vattrs, vencode

def add_encoding(ds, vencode=None):
    """
    Set valid_range attribute and encoding to every variable of the dataset.

    Parameters
    ----------
    ds: xr.Dataset
        Dataset of any processing level. The processing level will be
        determined by the global attribute 'processing_level'.
    vencode: dict or None
        Dictionary of encoding attributes by variable name, will be merged with pyrnet default cfmeta. The default is None.

    Returns
    -------
    xr.Dataset
        The input dataset but with encoding and valid_range attribute.
    """
    # prepare netcdf encoding
    _, vattrs_default, vencode_default = get_cfmeta()

    # Add valid range temporary to encoding dict.
    # As valid_range is not implemented in xarray encoding,
    #  it has to be stored as a variable attribute later.
    for k in vencode_default:
        if "valid_range" not in vencode_default[k]:
            continue
        vencode_default = assoc_in(vencode_default,
                                   [k,'valid_range'],
                                   vattrs_default['valid_range'])

    # merge input and default with preference on input
    if vencode is None:
        vencode = vencode_default
    else:
        a = vencode_default.copy()
        b = vencode.copy()
        vencode = {}
        for k in set(a)-set(b):
            vencode.update({k:a[k]})
        for k in set(a)&set(b):
            vencode.update({k: {**a[k],**b[k]}})
        for k in set(b)-set(a):
            vencode.update({k:b[k]})

    # add encoding to Dataset
    for k, v in vencode.items():
        if k not in ds.keys():
            continue
        ds[k].encoding = v
        if "valid_range" not in vencode[k]:
            continue
        # add valid_range to variable attributes
        ds[k].attrs.update({
            'valid_range': vencode[k]['valid_range']
        })

    # special treatment of time and flux variables
    if ds.processing_level=='l1a':
        # special treatment for flux variables
        for k in [var for var in ds.variables if var[:4] == 'dflx']:
            ds[k].encoding.update(vencode['sensor_voltage_mV200'])
            ds[k].attrs.update({
                'units': vattrs_default['sensor_voltage_mV200']['units'],
                'valid_range': vencode['sensor_voltage_mV200']['valid_range']
            })
    elif ds.processing_level == 'l1b':
        ds = stretch_resolution(ds)
    else:
        raise ValueError("Dataset has no 'processing_level' attribute.")

    # add time encoding
    ds["time"].encoding.update({
        "dtype": 'f8',
        "units": f"seconds since {np.datetime_as_string(ds.time.data[0], unit='D')}T00:00Z",
    })
    return ds
