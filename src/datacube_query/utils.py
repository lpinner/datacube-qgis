import os
from datetime import date

import dask.array
import numpy as np
import pandas as pd
import rasterio as rio
from rasterio.dtypes import check_dtype

import datacube
import datacube.api

from .defaults import (GTIFF_OVR_DEFAULTS,
                       GTIFF_COMPRESSION,
                       GTIFF_DEFAULTS,
                       GTIFF_OVR_RESAMPLING)

# This will get replaced with a git SHA1 when you do a git archive
__revision__ = '$Format:%H$'

# TODO Refactor and move qgis specific code to a separate module?
# TODO GeoTIFF options and overviews


def build_overviews(filename, overview_options):
    options = GTIFF_OVR_DEFAULTS.copy()
    if overview_options is not None:
        options.update(overview_options)
    if options['internal_storage']:
        mode = 'r+'
    else:
        mode = 'r'

    resampling = GTIFF_OVR_RESAMPLING[options['resampling']]
    with rio.open(filename, mode) as raster:
        raster.build_overviews(options['factors'], resampling)
        raster.update_tags(ns='rio_overview', resampling=options['resampling'])


def calc_stats(filename, stats_options):
    pass # TODO


def datetime_to_str(datetime64, str_format='%Y-%m-%d'):

    # datetime64 has nanosecond resolution so convert to millisecs
    dt = datetime64.astype(np.int64) // 1000000000

    dt = date.fromtimestamp(dt)
    return dt.strftime(str_format)


def get_products(config=None):
    # TODO
    dc = datacube.Datacube(config=config)
    products = dc.list_products()
    products = products[['name', 'description']]
    products.set_index('name', inplace=True)
    return products.to_dict('index')


def get_products_and_measurements(product=None, config=None):
    dc = datacube.Datacube(config=config)
    products = dc.list_products()
    measurements = dc.list_measurements()
    products = products.rename(columns={'name': 'product'})
    measurements.reset_index(inplace=True)
    display_columns = ['product_type', 'product', 'description']
    products = products[display_columns]
    display_columns = ['measurement', 'aliases', 'dtype', 'units', 'product']
    measurements = measurements[display_columns]
    prodmeas = pd.merge(products, measurements, how='left', on=['product'])
    if product is not None:
        prodmeas = prodmeas[prodmeas['product'] == product]
    prodmeas.set_index(['product_type', 'product', 'description'], inplace=True)  # , drop=False)
    return prodmeas


def lcase_dict(adict):
    ret_dict = {}
    for k, v in adict.iteritems():
        try:
            ret_dict[k.lower()] = v
        except TypeError:
            ret_dict[k] = v

    return ret_dict


def run_query(product, measurements, date_range, extent, query_crs,
              output_crs=None, output_res=None, config=None, dask_chunks=None):

    dc = datacube.Datacube(config=config, app='QGIS Plugin')

    xmin, ymin, xmax, ymax = extent
    query = dict(product=product, x=(xmin, xmax), y=(ymin, ymax), time=date_range, crs=str(query_crs))

    query_obj = datacube.api.query.Query(**query)
    # log_message(repr(query_obj.search_terms), 'index.datasets.search_eager')

    datasets = dc.index.datasets.search_eager(**query_obj.search_terms)

    if not datasets:
        raise RuntimeError('No datasets found')
        # return

    # TODO Masking
    # - test for PQ product
    # - apply default mask

    query['measurements'] = measurements
    query['dask_chunks'] = dask_chunks
    query['group_by'] = 'solar_day'
    if output_crs is not None:
        query['output_crs'] = str(output_crs)
    if output_res is not None:
        query['resolution'] = output_res

    log_message(repr(query), 'Query')

    data = dc.load(**query)

    return data


def str_snip(str_to_snip, max_len, suffix='...'):
    snip_len = max_len - len(suffix)
    snipped = str_to_snip if len(str_to_snip) <= max_len else str_to_snip[:snip_len]+suffix
    return snipped


def upcast(dataset, old_dtype):
    """ Upcast to next dtype of same kind, i.e. from int to int16 """

    old_dtype = np.dtype(old_dtype)  # Ensure old dtype is an np.dtype instance (i.e. not a string)
    dtype = np.dtype(old_dtype.kind + str(old_dtype.itemsize * 2))

    # dataset.astype strips attrs, so save them
    dataset_attrs = dataset.attrs
    datavar_attrs = {var: val.attrs for var, val in dataset.data_vars.items()}

    # Upcast
    dataset = dataset.astype(dtype) # loses attrs

    # Re-add attrs
    dataset.attrs.update(**dataset_attrs)
    for var, val in dataset.data_vars.items():
        val.attrs.update(**datavar_attrs[var])

    return dataset, dtype


def update_tags(filename, bidx=0, ns=None, **kwargs):
    with rio.open(filename, 'r+') as raster:
        raster.update_tags(bidx=bidx, ns=ns, **kwargs)


def write_geotiff(filename, dataset, time_index=None, profile_override=None):
    """
    Write an xarray dataset to a geotiff
        Modified from datacube.helpers.write_geotiff to support:
            - dask lazy arrays,
            - arrays with no time dimension
            - Nodata values
        https://github.com/opendatacube/datacube-core/blob/develop/datacube/helpers.py
        Original code licensed under the Apache License, Version 2.0 (the "License");

    :param filename: Output filename
    :attr dataset: xarray dataset containing multiple bands to write to file
    :attr time_index: time index to write to file
    :attr profile_override: option dict, overrides rasterio file creation options.

    """

    profile_override = profile_override or {}

    dtypes = {val.dtype for val in dataset.data_vars.values()}
    assert len(dtypes) == 1  # Check for multiple dtypes
    dtype = dtypes.pop()

    if not check_dtype(dtype):  # Check for invalid dtypes
        dataset, dtype = upcast(dataset, dtype)

    dimx = dataset.dims[dataset.crs.dimensions[1]]
    dimy = dataset.dims[dataset.crs.dimensions[0]]

    profile = GTIFF_DEFAULTS.copy()
    profile.update({
        'width': dimx,
        'height': dimy,
        'affine': dataset.affine,
        'crs': dataset.crs.crs_str,
        'count': len(dataset.data_vars),
        'dtype': str(dtype)
    })
    profile.update(profile_override)
    profile = lcase_dict(profile)

    blockx = profile.get('blockxsize')
    blocky = profile.get('blockysize')
    if (blockx and blockx > dimx) or (blocky and blocky > dimy):
        del profile['blockxsize']
        del profile['blockysize']
        profile['tiled'] = False

    with rio.open(str(filename), 'w', **profile) as dest:
        for bandnum, data in enumerate(dataset.data_vars.values(), start=1):
            try:
                nodata = data.nodata
                profile.update({'nodata': nodata})
            except AttributeError:
                pass

            if time_index is None:
                data = data.data
            else:
                data = data.isel(time=time_index).data

            if isinstance(data, dask.array.Array):
                data = data.compute()

            dest.write(data, bandnum)
