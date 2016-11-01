# -*- coding: utf-8 -*-
__author__='Daniel Scheffler'

import collections
import os
import tempfile
import time
import warnings

# custom
try:
    import gdal
except ImportError:
    from osgeo import gdal
import numpy as np
import rasterio

# internal modules
from py_tools_ds.ptds                      import GeoArray
from py_tools_ds.ptds.geo.map_info         import mapinfo2geotransform, geotransform2mapinfo
from py_tools_ds.ptds.geo.coord_grid       import is_coord_grid_equal
from py_tools_ds.ptds.geo.projection       import prj_equal
from py_tools_ds.ptds.geo.raster.reproject import warp_ndarray
from py_tools_ds.ptds.numeric.vector       import find_nearest
from py_tools_ds.ptds.processing.shell     import subcall_with_output

_dict_rspAlg_rsp_Int = {'nearest': 0, 'bilinear': 1, 'cubic': 2, 'cubic_spline': 3, 'lanczos': 4, 'average': 5,
                        'mode': 6, 'max': 7, 'min': 8 , 'med': 9, 'q1':10, 'q2':11}

class DESHIFTER(object):
    def __init__(self, im2shift, coreg_results, **kwargs):
        """
        Deshift an image array or one of its products by applying the coregistration info calculated by COREG class.

        :param im2shift:            <path,GeoArray> path of an image to be de-shifted or alternatively a GeoArray object
        :param coreg_results:       <dict> the results of the co-registration as given by COREG.coreg_info

        :Keyword Arguments:
            - path_out(str):        /output/directory/filename for coregistered results
            - fmt_out (str):        raster file format for output file. ignored if path_out is None. can be any GDAL
                                        compatible raster file format (e.g. 'ENVI', 'GeoTIFF'; default: ENVI)
            - band2process (int):   The index of the band to be processed within the given array (starts with 1),
                                    default = None (all bands are processed)
            - nodata(int, float):   no data value of an image to be de-shifted
            - out_gsd (float):      output pixel size in units of the reference coordinate system (default = pixel size
                                    of the input array), given values are overridden by match_gsd=True
            - align_grids (bool):   True: align the input coordinate grid to the reference (does not affect the
                                    output pixel size as long as input and output pixel sizes are compatible
                                    (5:30 or 10:30 but not 4:30), default = False
            - match_gsd (bool):     True: match the input pixel size to the reference pixel size,
                                    default = False
            - target_xyGrid(list):  a list with an x-grid and a y-grid like [[15,45], [15,45]]
            - resamp_alg(str)       the resampling algorithm to be used if neccessary
                                    (valid algorithms: nearest, bilinear, cubic, cubic_spline, lanczos, average, mode,
                                                       max, min, med, q1, q3)
            - warp_alg(str):        'GDAL_cmd' or 'GDAL_lib' (default = 'GDAL_lib')
            - cliptoextent (bool):  True: clip the input image to its actual bounds while deleting possible no data
                                    areas outside of the actual bounds, default = True
            - clipextent (list):    xmin, ymin, xmax, ymax - if given the calculation of the actual bounds is skipped.
                                    The given coordinates are automatically snapped to the output grid.
            - tempDir(str):         directory to be used for tempfiles (default: /dev/shm/)
            - CPUs(int):            number of CPUs to use (default: None, which means 'all CPUs available')
            - v(bool):              verbose mode (default: False)
            - q(bool):              quiet mode (default: False)

        """
        # unpack args
        self.im2shift           = im2shift if isinstance(im2shift, GeoArray) else GeoArray(im2shift)
        self.shift_prj          = self.im2shift.projection
        self.shift_gt           = list(self.im2shift.geotransform)
        self.GCPList            = coreg_results['GCPList'] if 'GCPList' in coreg_results else None
        mapI                    = coreg_results['updated map info']
        self.updated_map_info   = mapI if mapI else geotransform2mapinfo(self.shift_gt, self.shift_prj)
        self.original_map_info  = coreg_results['original map info']
        self.updated_gt         = mapinfo2geotransform(self.updated_map_info) if mapI else self.shift_gt
        self.ref_gt             = coreg_results['reference geotransform']
        self.ref_grid           = coreg_results['reference grid']
        self.ref_prj            = coreg_results['reference projection']
        self.updated_projection = self.ref_prj

        # unpack kwargs
        self.path_out     = kwargs.get('path_out'    , None)
        self.fmt_out      = kwargs.get('fmt_out'     , 'ENVI')
        self.band2process = kwargs.get('band2process', None) # starts with 1 # FIXME warum?
        self.nodata       = kwargs.get('nodata'      , self.im2shift.nodata)
        self.align_grids  = kwargs.get('align_grids' , False)
        tempAsENVI        = kwargs.get('tempAsENVI'  , False)
        self.outFmt       = 'VRT' if not tempAsENVI else 'ENVI' # FIXME eliminate that
        self.rspAlg       = kwargs.get('resamp_alg'  , 'cubic')
        self.warpAlg      = kwargs.get('warp_alg'    , 'GDAL_lib')
        self.cliptoextent = kwargs.get('cliptoextent', True)
        self.clipextent   = kwargs.get('clipextent'  , None)
        self.tempDir      = kwargs.get('tempDir'     , '/dev/shm/')
        self.CPUs         = kwargs.get('CPUs'        , None)
        self.v            = kwargs.get('v'           , False)
        self.q            = kwargs.get('q'           , False) if not self.v else False
        self.out_grid     = self._get_out_grid(kwargs) # needs self.ref_grid, self.im2shift
        self.out_gsd      = [abs(self.out_grid[0][1]-self.out_grid[0][0]), abs(self.out_grid[1][1]-self.out_grid[1][0])]  # xgsd, ygsd

        # assertions
        assert self.rspAlg  in _dict_rspAlg_rsp_Int.keys(), \
            "'%s' is not a supported resampling algorithm." %self.rspAlg
        assert self.warpAlg in ['GDAL_cmd', 'GDAL_lib']

        # set defaults for general class attributes
        self.is_shifted       = False # this is not included in COREG.coreg_info
        self.is_resampled     = False # this is not included in COREG.coreg_info
        self.tracked_errors   = []
        self.arr_shifted      = None  # set by self.correct_shifts
        self.GeoArray_shifted = None  # set by self.correct_shifts


    def _get_out_grid(self, init_kwargs):
        # parse given params
        out_gsd     = init_kwargs.get('out_gsd'      , None)
        match_gsd   = init_kwargs.get('match_gsd'    , False)
        out_grid    = init_kwargs.get('target_xyGrid', None)

        # assertions
        assert out_grid is None or (isinstance(out_grid,(list, tuple))      and len(out_grid)==2)
        assert out_gsd  is None or (isinstance(out_gsd, (int, tuple, list)) and len(out_gsd) ==2)

        ref_xgsd, ref_ygsd = (self.ref_grid[0][1]-self.ref_grid[0][0],self.ref_grid[1][1]-self.ref_grid[1][0])
        get_grid           = lambda gt, xgsd, ygsd: [[gt[0], gt[0] + xgsd], [gt[3], gt[3] - ygsd]]

        # get out_grid
        if out_grid:
            # output grid is given
            return out_grid

        elif out_gsd:
            out_xgsd, out_ygsd = [out_gsd, out_gsd] if isinstance(out_gsd, int) else out_gsd

            if match_gsd and (out_xgsd, out_ygsd)!=(ref_xgsd, ref_ygsd):
                warnings.warn("\nThe parameter 'match_gsd is ignored because another output ground sampling distance "
                              "was explicitly given.")
            if self.align_grids and self._grids_alignable(self.im2shift.xgsd, self.im2shift.ygsd, out_xgsd, out_ygsd):
                # use grid of reference image with the given output gsd
                return get_grid(self.ref_gt, out_xgsd, out_ygsd)
            else: # no grid alignment
                # use grid of input image with the given output gsd
                return get_grid(self.im2shift.geotransform, out_xgsd, out_ygsd)

        elif match_gsd:
            if self.align_grids:
                # use reference grid
                return self.ref_grid
            else:
                # use grid of input image and reference gsd
                return get_grid(self.im2shift.geotransform, ref_xgsd, ref_ygsd)

        else:
            if self.align_grids and self._grids_alignable(self.im2shift.xgsd, self.im2shift.ygsd, ref_xgsd, ref_ygsd):
                # use origin of reference image and gsd of input image
                return get_grid(self.ref_gt, self.im2shift.xgsd, self.im2shift.ygsd)
            else:
                # use input image grid
                return get_grid(self.im2shift.geotransform, self.im2shift.xgsd, self.im2shift.ygsd)


    @staticmethod
    def _grids_alignable(in_xgsd, in_ygsd, out_xgsd, out_ygsd):
        is_alignable = lambda gsd1, gsd2: max(gsd1, gsd2) % min(gsd1, gsd2) == 0  # checks if pixel sizes are divisible
        if not is_alignable(in_xgsd, out_xgsd) or not is_alignable(in_ygsd, out_ygsd):
            warnings.warn("\nThe targeted output coordinate grid is not alignable with the image to be shifted because "
                          "their pixel sizes are not exact multiples of each other (input [X/Y]: "
                          "%s %s; output [X/Y]: %s %s). Therefore the targeted output grid is "
                          "chosen for the resampled output image. If you don´t like that you can use the '-out_gsd' "
                          "parameter to set an appropriate output pixel size.\n"
                          % (in_xgsd, in_ygsd, out_xgsd, out_ygsd))
            return False
        else:
            return True


    def _get_out_extent(self):
        if self.cliptoextent and self.clipextent is None:
            self.clipextent        = self.im2shift.footprint_poly.bounds
        else:
            xmin, xmax, ymin, ymax = self.im2shift.box.boundsMap
            self.clipextent        = xmin, ymin, xmax, ymax


        # snap clipextent to output grid (in case of odd input coords the output coords are moved INSIDE the input array)
        xmin, ymin, xmax, ymax = self.clipextent
        xmin = find_nearest(self.out_grid[0], xmin, roundAlg='on' , extrapolate=True)
        ymin = find_nearest(self.out_grid[1], ymin, roundAlg='on' , extrapolate=True)
        xmax = find_nearest(self.out_grid[0], xmax, roundAlg='off', extrapolate=True)
        ymax = find_nearest(self.out_grid[0], ymax, roundAlg='off', extrapolate=True)
        return xmin, ymin, xmax, ymax


    def correct_shifts(self):
        # type: (DESHIFTER) -> collections.OrderedDict

        t_start   = time.time()
        equal_prj = prj_equal(self.ref_prj,self.shift_prj)

        if equal_prj and is_coord_grid_equal(self.shift_gt, *self.out_grid) and not self.align_grids:
            # FIXME buggy condition:
            # reconstructable with correct_spatial_shifts from GMS
            #DS = DESHIFTER(geoArr, self.coreg_info,
            #               target_xyGrid=[usecase.spatial_ref_gridx, usecase.spatial_ref_gridy],
            #               cliptoextent=True, clipextent=mapBounds, align_grids=False) => align grids False
            """NO RESAMPLING NEEDED"""
            self.is_shifted     = True
            self.is_resampled   = False
            xmin,ymin,xmax,ymax = self._get_out_extent()

            if self.cliptoextent: # TODO validate results!
                # get shifted array
                shifted_geoArr = GeoArray(self.im2shift[:],tuple(self.updated_gt), self.shift_prj)

                # clip with target extent
                self.arr_shifted, self.updated_gt, self.updated_projection = \
                        shifted_geoArr.get_mapPos((xmin,ymin,xmax,ymax), self.shift_prj, fillVal=self.nodata)
                self.updated_map_info = geotransform2mapinfo(self.updated_gt, self.updated_projection)
            else:
                # array keeps the same; updated gt and prj are taken from coreg_info
                self.arr_shifted = self.im2shift[:]
            self.GeoArray_shifted = GeoArray(self.arr_shifted, tuple(self.shift_gt), self.updated_projection)

            if self.path_out:
                GeoArray(self.arr_shifted,self.updated_gt,self.updated_projection).save(self.path_out,fmt=self.fmt_out)

        else: # FIXME equal_prj==False ist noch NICHT implementiert
            """RESAMPLING NEEDED"""
            if self.warpAlg=='GDAL_cmd':
                warnings.warn('This method has not been tested in its current state!')
                # FIXME nicht multiprocessing-fähig, weil immer kompletter array gewarpt wird und sich ergebnisse gegenseitig überschreiben
                # create tempfile
                fd, path_tmp = tempfile.mkstemp(prefix='CoReg_Sat', suffix=self.outFmt, dir=self.tempDir)
                os.close(fd)

                t_extent   = " -te %s %s %s %s" %self._get_out_extent()
                xgsd, ygsd = self.out_gsd
                cmd = "gdalwarp -r %s -tr %s %s -t_srs '%s' -of %s %s %s -srcnodata %s -dstnodata %s -overwrite%s"\
                      %(self.rspAlg, xgsd,ygsd,self.ref_prj,self.outFmt,self.im2shift.filePath,
                        path_tmp, self.nodata, self.nodata, t_extent)
                out, exitcode, err = subcall_with_output(cmd)

                if exitcode!=1 and os.path.exists(path_tmp):
                    """update map info, arr_shifted, geotransform and projection"""
                    ds_shifted = gdal.OpenShared(path_tmp) if self.outFmt == 'VRT' else gdal.Open(path_tmp)
                    self.shift_gt, self.shift_prj = ds_shifted.GetGeoTransform(), ds_shifted.GetProjection()
                    self.updated_map_info         = geotransform2mapinfo(self.shift_gt,self.shift_prj)

                    print('reading from', ds_shifted.GetDescription())
                    if self.band2process is None:
                        dim2RowsColsBands = lambda A: np.swapaxes(np.swapaxes(A,0,2),0,1) # rasterio.open(): [bands,rows,cols]
                        self.arr_shifted  = dim2RowsColsBands(rasterio.open(path_tmp).read())
                    else:
                        self.arr_shifted  = rasterio.open(path_tmp).read(self.band2process)

                    self.GeoArray_shifted = GeoArray(self.arr_shifted,tuple(self.shift_gt), self.shift_prj)
                    self.is_shifted       = True
                    self.is_resampled     = True

                    ds_shifted            = None
                    [gdal.Unlink(p) for p in [path_tmp] if os.path.exists(p)] # delete tempfiles
                else:
                    print("\n%s\nCommand was:  '%s'" %(err.decode('utf8'),cmd))
                    [gdal.Unlink(p) for p in [path_tmp] if os.path.exists(p)] # delete tempfiles
                    self.tracked_errors.append(RuntimeError('Resampling failed.'))
                    raise self.tracked_errors[-1]

                # TODO implement output writer

            elif self.warpAlg=='GDAL_lib':
                # apply XY-shifts to shift_gt
                in_arr = self.im2shift[self.band2process] if self.band2process else self.im2shift[:]
                if not self.GCPList:
                    self.shift_gt[0], self.shift_gt[3] = self.updated_gt[0], self.updated_gt[3]

                # get resampled array
                out_arr, out_gt, out_prj = \
                    warp_ndarray(in_arr, self.shift_gt, self.shift_prj, self.ref_prj,
                                 rspAlg     = _dict_rspAlg_rsp_Int[self.rspAlg],
                                 in_nodata  = self.nodata,
                                 out_nodata = self.nodata,
                                 out_gsd    = self.out_gsd,
                                 out_bounds = self._get_out_extent(),
                                 gcpList    = self.GCPList,
                                 polynomialOrder = None,
                                 options    = None,  #'-refine_gcps 500',
                                 CPUs       = self.CPUs,
                                 q          = self.q)

                self.updated_projection = out_prj
                self.arr_shifted        = out_arr
                self.updated_map_info   = geotransform2mapinfo(out_gt,out_prj)
                self.shift_gt           = mapinfo2geotransform(self.updated_map_info)
                self.GeoArray_shifted   = GeoArray(self.arr_shifted, tuple(self.shift_gt), self.updated_projection)
                self.is_shifted         = True
                self.is_resampled       = True

                if self.path_out:
                    GeoArray(out_arr, out_gt, out_prj).save(self.path_out,fmt=self.fmt_out)

        if self.v: print('Time for shift correction: %.2fs' %(time.time()-t_start))
        return self.deshift_results


    @property
    def deshift_results(self):
        deshift_results = collections.OrderedDict()
        deshift_results.update({'band'                : self.band2process})
        deshift_results.update({'is shifted'          : self.is_shifted})
        deshift_results.update({'is resampled'        : self.is_resampled})
        deshift_results.update({'updated map info'    : self.updated_map_info})
        deshift_results.update({'updated geotransform': self.shift_gt})
        deshift_results.update({'updated projection'  : self.updated_projection})
        deshift_results.update({'arr_shifted'         : self.arr_shifted})
        deshift_results.update({'GeoArray_shifted'    : self.GeoArray_shifted})
        return deshift_results