from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path

from datacube.utils import geometry
from sqlalchemy.exc import SQLAlchemyError
import processing

from processing.core.parameters import (
    QgsProcessingParameterCrs as ParameterCrs,
    QgsProcessingParameterEnum as ParameterEnum,
    QgsProcessingParameterExtent as ParameterExtent,
    QgsProcessingParameterNumber as ParameterNumber,
    QgsProcessingParameterFolderDestination as ParameterFolderDestination)

from processing.core.outputs import (
    QgsProcessingOutputMultipleLayers as OutputMultipleLayers)

from qgis.core import (
    QgsLogger,
    QgsProcessingContext,
    QgsProcessingException)

from .__base__ import BaseAlgorithm
from ..defaults import GROUP_BY_FUSE_FUNC
from ..exceptions import (NoDataError, TooManyDatasetsError)
from ..parameters import (ParameterDateRange, ParameterProducts)
from ..qgisutils import (get_icon)
from ..utils import (
    build_overviews,
    build_query,
    calculate_statistics,
    datetime_to_str,
    get_products_and_measurements,
    run_query,
    update_tags,
    write_geotiff
)


class DataCubeQueryAlgorithm(BaseAlgorithm):
    """
    Class that represent a "tool" in the processing toolbox.
    """

    OUTPUT_FOLDER = 'Output Directory'
    OUTPUT_LAYERS = 'Output Layers'

    PARAM_PRODUCTS = 'Products and measurements'
    PARAM_DATE_RANGE = 'Date range (yyyy-mm-dd)'
    PARAM_EXTENT = 'Query extent'

    # Advanced params
    PARAM_OVERVIEWS = 'Build overviews for output GeoTIFFs?'
    PARAM_OUTPUT_CRS = 'Output CRS (required for products with no CRS defined)'
    PARAM_OUTPUT_RESOLUTION = ('Output pixel resolution '
                               '(required for products with no resolution defined)')
    PARAM_GROUP_BY = 'Group data by'

    def __init__(self, products=None):
        """
        Initialise the algorithm

        :param dict products: A dict of products as returned by
            :func:`datacube_query.utils.get_products_and_measurements`
        """
        super().__init__()

        self._icon = get_icon('opendatacube.png')
        self.products = {} if products is None else products
        self.outputs = {}

    def checkParameterValues(self, parameters, context):

        msgs = []

        if self.parameterAsString(parameters, self.PARAM_PRODUCTS, context) == '{}':
            msgs += ['Please select at least one product']

        date_range = self.parameterAsString(parameters, self.PARAM_DATE_RANGE, context)
        date_range = json.loads(date_range)
        if not all(date_range) and not all([not d for d in date_range]):
            msgs += ['Please select two dates or none at all']

        if all(date_range):
            date_range = [datetime.strptime(d, '%Y-%m-%d') for d in date_range]
            if date_range[0] >  date_range[1]:
                msgs += ['The start date must be earlier than the end date']

        extent = self.parameterAsExtent(parameters, self.PARAM_EXTENT, context)  # QgsRectangle
        extent_crs = self.parameterAsExtentCrs(parameters, self.PARAM_EXTENT, context).authid()  # QgsCoordinateReferenceSystem
        extent = [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
        extent_crs = None if not extent_crs else extent_crs
        # Assume 4326 if within [-180,-90,180,90] and CRS not set
        if extent_crs is None:
            if not (extent[0] >= -180 and extent[1] >= -90 and extent[2] <= 180 and extent[3] <= 90):
                msgs += ['Please set a valid EPSG CRS for your project/layer']
        else:
            try:
                geometry.CRS(extent_crs)
            except geometry.InvalidCRSError:
                msgs += ['Please set a valid EPSG CRS for your project/layer']

        output_crs = self.parameterAsCrs(parameters, self.PARAM_OUTPUT_CRS, context)
        output_res = self.parameterAsDouble(parameters, self.PARAM_OUTPUT_RESOLUTION, context)
        if output_crs.isValid():
            if not output_res:
                msgs += ['Please specify "Output Resolution" when specifying "Output CRS"']
            try:
                geometry.CRS(output_crs.authid())
            except geometry.InvalidCRSError:
                msgs += ['Please set a valid EPSG "Output CRS"']

        if msgs:
            return False, self.tr('\n'.join(msgs))

        return super().checkParameterValues(parameters, context)

    def createInstance(self, config=None):
        try:
            products = self.get_products_and_measurements()
        except SQLAlchemyError:
            msg = 'Unable to connect to a running Data Cube instance'
            QgsLogger().warning(msg)
            products = {msg: {'measurements': {}}}

        return type(self)(products)

    def displayName(self, *args, **kwargs):
        return self.tr('Data Cube Query')

    def flags(self):
        # Default is FlagCanCancel | FlagSupportsBatch
        # but this alg looks bad in batch mode because of the big tree widget.
        # Not sure why, but setting this doesn't actually
        # stop the "Run As Batch Process..." button from being shown.

        # return self.FlagCanCancel
        return super().flags() & ~self.FlagSupportsBatch

    def get_products_and_measurements(self):
        config_file = self.get_settings()['datacube_config_file'] or None
        return get_products_and_measurements(config=config_file)

    def group(self):
        """
        The folder the tool is shown in
        """
        return self.tr('Data Cube Query')

    def groupId(self):
        return 'datacubequery'

    def initAlgorithm(self, config=None):
        """
        Define the parameters and output of the algorithm.
        """

        # Basic Params
        items = defaultdict(list)
        for k, v in self.products.items():
            items[k] += v['measurements'].keys()

        self.addParameter(ParameterProducts(self.PARAM_PRODUCTS,
                                            self.tr(self.PARAM_PRODUCTS),
                                            items=items))

        self.addParameter(ParameterDateRange(self.PARAM_DATE_RANGE,
                                             self.tr(self.PARAM_DATE_RANGE),
                                             optional=True))
        self.addParameter(ParameterExtent(self.PARAM_EXTENT,
                                          self.tr(self.PARAM_EXTENT)))

        param = ParameterCrs(self.PARAM_OUTPUT_CRS, self.tr(self.PARAM_OUTPUT_CRS), optional=True)
        self.addParameter(param)

        param = ParameterNumber(self.PARAM_OUTPUT_RESOLUTION, self.tr(self.PARAM_OUTPUT_RESOLUTION),
                                type=ParameterNumber.Double, optional=True, defaultValue=None)
        self.addParameter(param)

        param = ParameterEnum(self.PARAM_GROUP_BY, self.tr(self.PARAM_GROUP_BY), allowMultiple=False,
                              options=GROUP_BY_FUSE_FUNC.keys(), defaultValue=0)
        self.addParameter(param)

        # Output/s
        self.addParameter(ParameterFolderDestination(self.OUTPUT_FOLDER,
                                                     self.tr(self.OUTPUT_FOLDER)),
                          createOutput=True)

        self.addOutput(OutputMultipleLayers(self.OUTPUT_LAYERS, self.tr(self.OUTPUT_LAYERS)))

    def prepareAlgorithm(self, parameters, context, feedback):
        return True

    def postProcessAlgorithm(self, context, feedback):
        """
        Add resulting layers to map

        :param qgis.core.QgsProcessingContext context:  Threadsafe context in which a processing algorithm is executed
        :param qgis.core.QgsProcessingFeedback feedback: For providing feedback from a processing algorithm
        """
        output_layers = self.outputs if self.outputs else {}

        for layer, layer_name in output_layers.items():
            context.addLayerToLoadOnCompletion(
                layer, QgsProcessingContext.LayerDetails(layer_name, context.project()))
        return {} #Avoid NoneType can not be converted to a QMap instance

    # noinspection PyMethodOverriding
    def processAlgorithm(self, parameters, context, feedback):
        """
        Collect parameterfeedback.setProgressfeedback.setProgressfeedback.setProgresss and execute the query

        :param parameters: Input parameters supplied by the processing framework
        :param qgis.core.QgsProcessingContext context:  Threadsafe context in which a processing algorithm is executed
        :param qgis.core.QgsProcessingFeedback feedback: For providing feedback from a processing algorithm
        :return:
        """

        # General options
        settings = self.get_settings()
        config_file = settings['datacube_config_file'] or None
        try:
            max_datasets = int(settings['datacube_max_datasets'])
        except (TypeError, ValueError):
            max_datasets = None
        gtiff_options = json.loads(settings['datacube_gtiff_options'])
        gtiff_ovr_options = json.loads(settings['datacube_gtiff_ovr_options'])
        overviews = settings['datacube_build_overviews']
        calc_stats = settings['datacube_calculate_statistics']
        approx_ok = settings['datacube_approx_statistics']

        # Parameters
        product_descs = self.parameterAsString(parameters, self.PARAM_PRODUCTS, context)
        product_descs = json.loads(product_descs)
        products = defaultdict(list)
        for k, v in product_descs.items():
            for m in v:
                products[self.products[k]['product']] += [self.products[k]['measurements'][m]]

        date_range = self.parameterAsString(parameters, self.PARAM_DATE_RANGE, context)
        date_range = json.loads(date_range)
        date_range = date_range if all(date_range) else None

        extent = self.parameterAsExtent(parameters, self.PARAM_EXTENT, context)  # QgsRectangle
        extent = [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
        extent_crs = self.parameterAsExtentCrs(parameters, self.PARAM_EXTENT, context)  # QgsCoordinateReferenceSystem
        extent_crs = extent_crs.authid()
        extent_crs = None if not extent_crs else extent_crs

        output_crs = self.parameterAsCrs(parameters, self.PARAM_OUTPUT_CRS, context).authid()
        output_crs = None if not output_crs else output_crs

        output_res = self.parameterAsDouble(parameters, self.PARAM_OUTPUT_RESOLUTION, context)
        output_res = None if not output_res else [output_res, output_res]

        group_by = self.parameterAsEnum(parameters, self.PARAM_GROUP_BY, context)
        group_by, fuse_func = GROUP_BY_FUSE_FUNC[list(GROUP_BY_FUSE_FUNC.keys())[group_by]]

        output_folder = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        feedback.pushInfo('output_folder: {}'.format(repr(output_folder)))

        processing.mkdir(output_folder)

        dask_chunks = {'time': 1} if date_range is not None else None

        output_layers = self.execute(
            products, date_range, extent, extent_crs,
            output_crs, output_res, output_folder,
            config_file, dask_chunks, overviews, calc_stats, approx_ok,
            gtiff_options, gtiff_ovr_options,
            group_by, fuse_func, max_datasets, feedback)

        results = {self.OUTPUT_FOLDER: output_folder, self.OUTPUT_LAYERS: output_layers.keys()}
        self.outputs = output_layers # This is used in postProcessAlgorithm
        return results

    # noinspection PyTypeChecker
    def execute(self,
                products, date_range, extent, extent_crs,
                output_crs, output_res, output_folder,
                config_file, dask_chunks, overviews, calc_stats, approx_ok,
                gtiff_options, gtiff_ovr_options,
                group_by, fuse_func, max_datasets, feedback):

        output_layers = {}
        progress_total = 100 / (10*len(products))
        feedback.setProgress(0)

        for idx, (product, measurements) in enumerate(products.items()):

            if feedback.isCanceled():
                return output_layers

            feedback.setProgressText('Processing {}'.format(product))

            try:
                query = build_query(
                    product, measurements,
                    date_range, extent,
                    extent_crs, output_crs,
                    output_res, dask_chunks=dask_chunks,
                    group_by=group_by, fuse_func=fuse_func)

                # feedback.setProgressText('Query {}'.format(repr(query)))
                feedback.pushInfo('Query: {}'.format(repr(query)))

                data = run_query(query, config_file, max_datasets=max_datasets)

            except (NoDataError, TooManyDatasetsError, OSError) as err:
                feedback.reportError('Error encountered processing {}: {}'.format(product, err))
                feedback.setProgress(int((idx + 1) * 10 * progress_total))
                continue

            basename = '{}_{}'.format(product, '{}')
            basepath = str(Path(output_folder, basename))

            feedback.setProgressText('Saving outputs for {}'.format(product))
            for i, dt in enumerate(data.time):
                if group_by is None:
                    ds = datetime_to_str(dt.data, '%Y-%m-%d_%H-%M-%S')
                    tag = datetime_to_str(dt.data, '%Y:%m:%d %H:%M:%S')
                else:
                    ds = datetime_to_str(dt.data)
                    tag = datetime_to_str(dt.data, '%Y:%m:%d')

                raster_path = basepath.format(ds) + '.tif'

                write_geotiff(data, raster_path, time_index=i,
                              profile_override=gtiff_options, overwrite=True)

                update_tags(raster_path, TIFFTAG_DATETIME=tag)

                if overviews:
                    build_overviews(raster_path, gtiff_ovr_options)

                if calc_stats:
                    calculate_statistics(raster_path, approx_ok)

                lyr_name = basename.format(ds)
                output_layers[raster_path] = lyr_name

                feedback.setProgress(int((idx * 10 + i + 1) * progress_total))

                if feedback.isCanceled():
                    return output_layers

            feedback.setProgress(int((idx + 1) * 10 * progress_total))

        return output_layers
