# Streamer class
import os.path
import time
from datetime import date, datetime, timedelta
import numpy.ma
import numpy as np
from s2cloudless import S2PixelCloudDetector
from scipy.interpolate import UnivariateSpline
from sklearn.gaussian_process import GaussianProcessRegressor

from sentinelhub.data_request import WcsRequest
from sentinelhub.constants import MimeType
from sentinelhub.common import BBox, CRS
from sentinelhub import CustomUrlParam

# INSTANCE_ID = open("INSTANCE_ID").read()
# Fuck it
from typing import Tuple, Optional, List

from kafka import KafkaProducer

from JsonSerializer import encode

INSTANCE_ID = "b1062c36-3d9a-4df5-ad3d-ab0d40ae3ca0"
TULIP_FIELD_COORDINATES = 4.798278808593751, 52.95205098150524, 4.71038818359375, 52.89906593845727,


class CloudDetectionSettings:
    """
    Settings for Cloud detection using Sinergise cloudless library,
    x_scale and y_scale are used for rescaling
    (detecting clouds on same image takes A LOT of time),
    so it is best to rescale it before
    other arguments are directly forwarded to cloud detector
    """

    def __init__(self,
                 threshold: float = 0.4,
                 average_over: int = 4,
                 dilation_size: int = 2,
                 x_scale: int = 6,
                 y_scale: int = 6,
                 ):
        self.threshold = threshold
        self.average_over = average_over
        self.dilation_size = dilation_size
        self.x_scale = x_scale
        self.y_scale = y_scale


DEFAULT_CLOUD_DETECTION_SETTINGS = CloudDetectionSettings()


class DataAcquisitionSettings:
    """
    Main settings class
    This class customizes data acquisition and it's saving
    """

    def __init__(self,
                 coordinates: Tuple[float, float, float, float],
                 *,
                 interpolate: bool = True,
                 data_folder_name: str = "data/",
                 stream_data_folder_name: str = os.path.join("stream_data"),
                 start_date: str = "2017-01-01",
                 end_date: str = "2017-12-31",
                 res_x: int = 60, res_y: int = 60,
                 redownload: bool = False,
                 instance_id: str = INSTANCE_ID,
                 cloud_detection_settings:
                 Optional[CloudDetectionSettings] = None
                 ):
        self.interpolate = interpolate
        self.coordinates = coordinates
        self.bbox = BBox(self.coordinates, crs=CRS.WGS84)
        self.data_folder_name = data_folder_name
        self.stream_data_folder_name = stream_data_folder_name
        self.start_date = start_date
        self.end_date = end_date
        self.time_range = self.start_date, self.end_date
        self.res_x = res_x
        self.res_y = res_y
        self.redownload = redownload
        self.instance_id = instance_id
        if cloud_detection_settings is None:
            self.cloud_detection_settings = DEFAULT_CLOUD_DETECTION_SETTINGS
        else:
            self.cloud_detection_settings = cloud_detection_settings


class DataAcquirer:
    """
    Main data acquisiton class
    """

    def __init__(self, name: str,
                 settings: Optional[DataAcquisitionSettings] = None,
                 split_num: int = 50):
        """
        Constructs class and prepares everything for usage
        :param name: name to be used for saving data on disk
        :param settings: settings class for data acquisition
        :type settings: DataAcquisitionSettings
        :param split_num:
        """
        self.name = name
        self.settings = settings or DataAcquisitionSettings(
            TULIP_FIELD_COORDINATES)
        self.split_num = split_num
        self.data_dimensions = -1, -1, -1, -1
        self.full_dates = []  # type: List[date]

        self.acquire_finished = False

        # Iteration data
        self.iteration_index = -1

    def create_requests(self):
        # Get all bands
        all_bands_request = WcsRequest(
            layer="ALL-BANDS",
            data_folder=self.settings.data_folder_name,
            custom_url_params={CustomUrlParam.SHOWLOGO: False},
            bbox=BBox(bbox=self.settings.bbox, crs=CRS.WGS84),
            time=self.settings.time_range,
            resx=str(self.settings.res_x) + "m",
            resy=str(self.settings.res_y) + "m",
            image_format=MimeType.TIFF_d32f,
            instance_id=self.settings.instance_id
        )

        if (self.settings.cloud_detection_settings.x_scale == 1 and
                self.settings.cloud_detection_settings.y_scale == 1):
            cloud_bands_request = all_bands_request
        else:
            cloud_bbox = list(self.settings.coordinates)
            # To get one more pixel when downloading clouds
            # Note: Numbers are not precise
            cloud_bbox[2] += 0.001
            cloud_bbox[3] += 0.001
            cloud_bbox = tuple(cloud_bbox)

            cloud_bands_request = WcsRequest(
                layer='ALL-BANDS',
                data_folder=
                self.settings.data_folder_name,
                custom_url_params={CustomUrlParam.SHOWLOGO: False},
                bbox=BBox(bbox=cloud_bbox, crs=CRS.WGS84),
                time=self.settings.time_range,
                resx=str(self.settings.cloud_detection_settings.x_scale) + "m",
                resy=str(self.settings.cloud_detection_settings.y_scale) + "m",
                image_format=MimeType.TIFF_d32f,
                instance_id=self.settings.instance_id
            )

        return all_bands_request, cloud_bands_request

    @staticmethod
    def upscale_image(img, scale):
        return np.kron(img, np.ones((scale, scale)))

    def load_eo_data(self):
        all_bands_request, cloud_bands_request = self.create_requests()

        all_bands = all_bands_request.get_data(
            save_data=True,
            redownload=self.settings.redownload
        )
        print("downloaded all bands")

        cloud_bands = cloud_bands_request.get_data(
            save_data=True,
            redownload=self.settings.redownload
        )
        print("downloaded cloud bands")
        print(cloud_bands[0].shape)

        dates1 = all_bands_request.get_dates()
        dates = [j.date() for j in dates1]
        out = []
        for j in range(1, len(dates)):
            if dates[j] == dates[j - 1]:
                out.append(j)

        all_bands = np.delete(all_bands, out, 0)
        cloud_detector = S2PixelCloudDetector(average_over=self.settings.cloud_detection_settings.average_over,
                                              dilation_size=self.settings.cloud_detection_settings.dilation_size,
                                              threshold=self.settings.cloud_detection_settings.threshold,
                                              all_bands=True)
        cloud_masks_orig = cloud_detector.get_cloud_masks(np.array(all_bands))
        # upscale cloud masks
        cloud_masks = []
        for i in range(len(cloud_masks_orig)):
            cloud_masks.append(self.upscale_image(cloud_masks_orig[i], self.settings.cloud_detection_settings.x_scale))
        cloud_masks = np.array(cloud_masks)

        for j in out:
            dates1.pop(j)

        return all_bands, cloud_masks, dates1

    def resample_interpolate_save(self):
        os.makedirs(self.full_spline_data_folder_name, exist_ok=True)
        os.makedirs(self.full_kriging_data_folder_name, exist_ok=True)
        os.makedirs(self.spline_file_name, exist_ok=True)
        eo_data, cloud_data, dates1 = self.load_eo_data()
        eo_data = np.array(eo_data)
        dates = [j.date() for j in dates1]  # type: List[date]

        interpolate = self.settings.interpolate

        start_date = datetime.strptime(self.settings.start_date,
                                       "%Y-%m-%d").date()
        end_date = datetime.strptime(self.settings.end_date, "%Y-%m-%d").date()
        delta = (end_date - start_date).days

        h, w, d = eo_data[0].shape
        self.data_dimensions = eo_data.shape
        assert d == 13
        print(eo_data.shape)

        available = np.array([(j - start_date).days for j in dates])
        available2 = available.reshape(-1, 1)

        x = np.arange(0, delta, 1)
        xx = x.reshape(-1, 1)

        self.full_dates = np.arange(start_date, end_date)

        # full_data = np.zeros((delta, *eo_data[0].shape), dtype=float)
        spline_interpolants = np.zeros((w, h, d), dtype=object)
        kriging_interpolants = np.zeros((w, h, d), dtype=object)

        t = time.time()
        for i in range(h):
            print("Line:", i, end=" ", flush=True)
            tt = time.time()
            line_spline = np.zeros((delta, h, d), dtype=float)
            line_kriging = np.zeros((delta, h, d), dtype=float)
            for j in range(w):
                for k in range(d):
                    current_pixel_data = eo_data[:, i, j, k]
                    masked_pixel_data = np.ma.masked_array(current_pixel_data,
                                                           cloud_data[:,i,j])
                    if interpolate:
                        # Gaussian is quite slow
                        gp = GaussianProcessRegressor()
                        gp.fit(X=available2, y=masked_pixel_data)
                        # kriging_interpolants[i, j, k] = gp

                        spline = UnivariateSpline(available, masked_pixel_data,
                                                  s=0)
                        # spline_interpolants[i, j, k] = spline

                        line_spline[:, j, k] = spline(x)
                        line_kriging[:, j, k] = gp.predict(xx)
                        # full_data[:, i, j, k] = spline(x)
                    else:
                        line_spline[:, j, k] = np.nan
                        cur = np.ma.masked_array(available,
                                                 masked_pixel_data.mask)
                        line_spline[available[~cur.mask],j,k] = masked_pixel_data[~cur.mask]

            self.split_save_to_file(self.full_spline_data_folder_name, i,
                                    line_spline)
            self.split_save_to_file(self.full_kriging_data_folder_name, i,
                                    line_kriging)
            print(time.time() - tt)
        print("Interpolation ended:", time.time() - t)

        t = time.time()
        print("Saving spline:", end="", flush=True)
        np.save(self.spline_file_name, spline_interpolants)
        print(time.time() - t)
        t = time.time()
        print("Saving kriging:", end="", flush=True)
        np.save(self.kriging_file_name, kriging_interpolants)
        print(time.time() - t)

        np.save(
            os.path.join(self.settings.stream_data_folder_name, self.name,
                         "dates"),
            self.full_dates
        )

        self.restructure_files(self.full_kriging_data_folder_name, h, w, delta)
        self.restructure_files(self.full_spline_data_folder_name, h, w, delta)

        self.save_final_state()

    def get_data(self, recalculate: bool = False):
        if recalculate or not os.path.exists(self.final_file_name):
            self.resample_interpolate_save()

        self.full_dates = np.load(
            os.path.join(self.settings.stream_data_folder_name, self.name,
                         "dates.npy")
        )
        self.acquire_finished = True

    @property
    def spline_file_name(self):
        return os.path.join(self.settings.stream_data_folder_name,
                            self.name, "interpolants", "spline")

    @property
    def kriging_file_name(self):
        return os.path.join(self.settings.stream_data_folder_name,
                            self.name, "interpolants", "kriging")

    def interpolated_data_folder_name(self, interpolation_type: str):
        return os.path.join(self.settings.stream_data_folder_name,
                            self.name, "full_data", interpolation_type)

    @property
    def full_spline_data_folder_name(self):
        return self.interpolated_data_folder_name("spline")

    @property
    def full_kriging_data_folder_name(self):
        return self.interpolated_data_folder_name("kriging")

    @property
    def final_file_name(self):
        return os.path.join(self.settings.stream_data_folder_name, self.name,
                            "final.txt")

    def save_final_state(self):
        today = datetime.today()
        open(self.final_file_name, "w").write(str(today))

    def split_save_to_file(self, folder: str, line_num: int, data):
        split = self.split_num
        for j in range(len(data) // split):
            np.save(
                os.path.join(folder, "tmp-" + str(line_num) + "-" + str(j)),
                data[j * split:(j + 1) * split])
        if len(data) % split:  # Take remainder
            j = len(data) // split
            np.save(
                os.path.join(folder, "tmp-" + str(line_num) + "-" + str(j)),
                data[j * split:])

    def restructure_files(self, folder: str, h: int, w: int, l: int):
        print("Resturcturing: " + folder)
        t = time.time()
        split = self.split_num
        begin = os.path.join(folder, "tmp-")
        for j in range(l // split):
            time_slice = np.zeros((split, w, h, 13), dtype=float)
            for i in range(h):
                data = np.load(begin + str(i) + "-" + str(j) + ".npy")
                time_slice[:, i, :, :] = data
            np.save(os.path.join(folder, "final-") + str(j), time_slice)

        if l % split:  # Take remainder
            j = l // split
            time_slice = np.zeros((l % split, w, h, 13), dtype=float)
            for i in range(h):
                data = np.load(begin + str(i) + "-" + str(j) + ".npy")
                time_slice[:, i, :, :] = data
            np.save(os.path.join(folder, "final-") + str(j), time_slice)
        print(time.time() - t)

    def __iter__(self):
        if not self.acquire_finished:
            raise ValueError("First initialize data using: .get_data()")
        # Reset
        self.iteration_index = 0
        return self

    def __next__(self):
        t, _, _, _ = self.data_dimensions
        if self.iteration_index >= len(self.full_dates):
            raise StopIteration

        tmp = self.get_data_at_index(self.iteration_index,
                                     self.full_spline_data_folder_name)
        tmp_date = self.full_dates[self.iteration_index]
        self.iteration_index += 1

        return tmp_date, tmp

    def get_data_at_index(self, index: int, folder: str):
        assert self.acquire_finished
        split = self.split_num
        j = index // split
        k = index % split
        data = np.load(os.path.join(folder, "final-" + str(j) + ".npy"))
        return data[k, :, :, :]


class Streamer:
    """
    Main Streamer class

    """

    def __init__(self, name: str, kafka_config, topic_name="PerceptiveSentinel",
                 flush=True, sleep_time=0, serializer=encode,
                 daq_settings=None):
        """

        :param name: name used in DataAcquirer to
        :param kafka_config: kafka configuration -> forwarded to kafka client
        :param topic_name: name of kafka topic to send to
        :param flush: flush after each send
        :param sleep_time: time between two days
        :param serializer: serializer to use for serialization
        """
        self.name = name
        self.data_acquirer = DataAcquirer(
            self.name,
            DataAcquisitionSettings(**(daq_settings or {}))
        )
        self.kafka_producer = KafkaProducer(**kafka_config)
        self.topic_name = topic_name
        self.serializer = serializer
        self.flush = flush
        self.sleep_time = sleep_time if sleep_time > 0 else 0

    def start(self):
        self.data_acquirer.get_data()
        for ind, (item_date, item) in enumerate(self.data_acquirer):
            print(ind, item_date, np.mean(item), np.std(item))
            data = {
                "date": item_date,
                "data": [np.mean(item), np.std(item)],
                "full_data": item,
            }
            data = self.serializer(data)
            self.kafka_producer.send(
                self.topic_name,
                bytes(data, encoding="utf8")
            )
            if self.flush:
                self.kafka_producer.flush()
            time.sleep(self.sleep_time)
        self.kafka_producer.flush()
