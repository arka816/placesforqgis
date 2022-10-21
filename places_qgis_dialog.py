# -*- coding: utf-8 -*-
"""
    places for qgis
    Author: Arkaprava Ghosh
    Mail:   arkaprava.mail@gmail.com
"""

import os
import requests
import pandas as pd
import xlsxwriter
from datetime import datetime
import numpy as np
import time

from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox
from qgis.PyQt.QtCore import QObject, QThread, pyqtSignal, QVariant
from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsField, QgsMarkerSymbol

from PyQt5.QtWebKitWidgets import QWebView

XLSX_COL_WIDTHS = {
    'A': 2,
    'B': 15,
    'C': 15,
    'D': 25,
    'E': 30,
    'F': 30,
    'G': 25,
    'H': 100,
    'I': 30
}

METADATA_DOWNLOAD_PROGRESS = 10
IMAGE_DOWNLOAD_PROGRESS = 30
NPT_VALIDITY_DELAY = 5      # time taken for next page token to be valid after being issued
CHUNK_SIZE = 4096           # chunk size for files

# This loads your .ui file so that PyQt can populate your plugin with the elements from Qt Designer
FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'places_qgis_dialog_base.ui'))


class PlacesQgisDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, parent=None):
        """Constructor."""
        super(PlacesQgisDialog, self).__init__(parent)
        # Set up the user interface from Designer through FORM_CLASS.
        # After self.setupUi() you can access any designer object by doing
        # self.<objectname>, and you can use autoconnect slots - see
        # http://qt-project.org/doc/qt-4.8/designer-using-a-ui-file.html
        # #widgets-and-dialogs-with-auto-connect
        self.setupUi(self)

        # initialize flags and qt elements
        # set download in progress flag as false
        self.isDownloadInProgress = False

        # set logbox empty
        self.logBox.setPlainText("")

        # set progress bar to zero
        self.progressBar.setValue(0)

        # disable stop button
        self.stopButton.setEnabled(False)

        self.elem_config_map = {
            'GAPI_KEY' : self.gapiKey,
            'XLSX_FILE_PATH': self.xlsxFilePath,
            'OUTPUT_DIR_NAME': self.outputDirName,
            'LATITUDE': self.latitude,
            'LONGITUDE': self.longitude,
            'RADIUS': self.radius,
            'KEYWORD': self.keyword,
            'SAVE_LOG': self.saveLogCheck,
            'SAVE_IMAGES': self.saveImages,
            'LIMIT_ENTRIES': self.limitEntries
        }

        self.api_report_map = {
            'NEARBY': self.nearbysearchUsage,
            'REVIEWS': self.reviewsUsage,
            'PHOTOS': self.photosUsage
        }

        self.configFilePath = os.path.join(os.path.dirname(__file__), ".conf")
        self.logFilePath = os.path.join(os.path.dirname(__file__), ".logfile")
        self.usageFilePath = os.path.join(os.path.dirname(__file__), "usage.dat")

        # connect buttons to handler
        self.startButton.clicked.connect(self._start_download_thread)
        self.stopButton.clicked.connect(self._stop_download_thread)
        self.xlsxFilePicker.clicked.connect(self._select_xlsx_file)
        self.closeWindows.clicked.connect(self._close_browser_windows)
        self.removeLayers.clicked.connect(self._remove_layers)
        self.outputDirPicker.clicked.connect(self._select_output_folder)

        # load previous user input
        self._load_prev_input()

        # load api usage data
        self._show_api_usage()

        # connect to input saver, cleanup and log saver
        self.rejected.connect(self._save_input)
        self.rejected.connect(self._cleanup)
        self.rejected.connect(self._save_log)

    def _save_log(self):
        if self.saveLogCheck.isChecked():
            try:
                f = open(self.logFilePath, 'w')
            except:
                return

            f.write(self.logBox.toPlainText())
            f.close()
        return

    def _select_xlsx_file(self):
        xlsxFilePath, _ = QFileDialog.getSaveFileName(self, "choose xlsx file", "", "*.xlsx")
        self.xlsxFilePath.setText(xlsxFilePath)

    def _select_output_folder(self):
        outputDir = QFileDialog.getExistingDirectory(self, "choose output directory")
        self.outputDirName.setText(outputDir)

    def _cleanup(self):
        # clean vector layer
        self._remove_layers()

        # close open browser windows
        self._close_browser_windows()

    def _close_browser_windows(self):
        if hasattr(self, 'webViews'):
            for webView in self.webViews:
                try:
                    webView.close()
                except:
                    pass

    def _remove_layers(self):
        try:
            QgsProject.instance().removeMapLayers([self.boundaryLayer.id(), self.markerLayer.id()])
            QgsProject.instance().refreshAllLayers()
        except:
            pass

    def _save_input(self):
        try:
            f = open(self.configFilePath, 'w')
        except:
            return
        
        l = list()

        for key, val in self.elem_config_map.items():
            if key == 'SAVE_LOG' or key == 'SAVE_IMAGES':
                l.append(f"{key}={'true' if val.isChecked() else 'false'}")
            else:
                l.append(f"{key}={val.text()}")

        f.write('\n'.join(l))
        f.close()
        return

    def _load_prev_input(self):
        if os.path.exists(self.configFilePath):
            # load configurations from configfile
            try:
                f = open(self.configFilePath)
            except:
                self.logBox.append("Error: could not load from config file.")
                return

            for line in f.readlines():
                key, val = line.strip('\n').split("=")
                elem = self.elem_config_map[key]

                if key == 'SAVE_LOG' or key == 'SAVE_IMAGES':
                    elem.setChecked(val == "true")
                else:    
                    elem.setText(val)

            f.close()
            return

    def _start_download_thread(self):
        self._cleanup()
        self.progressBar.setValue(0)

        def float_error(elem, elem_name):
            QMessageBox.warning(self, "Error", f"{elem_name} is not numeric")
            elem.setFocus()
            elem.selectAll()

        def lat_error(elem):
            QMessageBox.warning(self, "Error", "latitude must lie between -90 and 90 degrees")
            elem.setFocus()
            elem.selectAll()

        def long_error(elem):
            QMessageBox.warning(self, "Error", "longitude must lie between -180 and 180 degrees")
            elem.setFocus()
            elem.selectAll()

        def rad_error(elem):
            QMessageBox.warning(self, "Error", "radius must lie between 0 and 50 kms")
            elem.setFocus()
            elem.selectAll()

        def limit_error(elem):
            QMessageBox.warning(self, "Error", "entry limit cannot be negative")
            elem.setFocus()
            elem.selectAll()

        if not self.isDownloadInProgress:
            # collect data
            try:
                latitude = float(self.latitude.text())
            except Exception as ex:
                float_error(self.latitude, "latitude")

            if not (-90 <= latitude <= 90):
                lat_error(self.latitude)
                
            try:
                longitude = float(self.longitude.text())
            except Exception as ex:
                float_error(self.longitude, "longitude")

            if not (-180 <= longitude <= 180):
                long_error(self.longitude)

            try:
                limitEntries = int(self.limitEntries.text())
            except Exception as ex:
                float_error(self.limitEntries, "limit entries")

            if limitEntries < 0:
                limit_error(self.limitEntries)

            try:
                radius = int(self.radius.text())
            except Exception as ex:
                float_error(self.radius, "radius")

            if not (0 <= radius <= 50):
                rad_error(self.radius)

            gapiKey = self.gapiKey.text()
            keyword = self.keyword.text()
            xlsxFilePath = self.xlsxFilePath.text()
            outputDirName = self.outputDirName.text()

            if len(gapiKey) == 0:
                QMessageBox.warning(self, "Error", "places api key needs to be specified")
                self.gapiKey.setFocus()

            if len(keyword) == 0:
                QMessageBox.warning(self, "Error", "keyword needs to be specified")
                self.keyword.setFocus()

            if len(xlsxFilePath) == 0:
                QMessageBox.warning(self, "Error", "xlsx file path needs to be specified")
                self.xlsxFilePath.setFocus()

            if len(outputDirName) == 0:
                QMessageBox.warning(self, "Error", "output directory needs to be specified")
                self.outputDirName.setFocus()

            
            if ('latitude' in locals()) and ('longitude' in locals()) and ('radius' in locals()) and\
                -180 <= longitude <= 180 and -90 <= latitude <= 90 and limitEntries >= 0 and\
                len(gapiKey) != 0 and len(keyword) != 0 and len(xlsxFilePath) != 0 and len(outputDirName) != 0:

                # no error in input; set download thread in progress
                self.isDownloadInProgress = True

                # enable and disable start and stop buttons
                self.startButton.setEnabled(False)
                self.stopButton.setEnabled(True)

                # clear log box
                self.logBox.clear()

                # create worker
                self.thread = QThread()
                self.worker = Worker(latitude, longitude, radius, xlsxFilePath, gapiKey, keyword, outputDirName, self.saveImages.isChecked(), limitEntries)
                self.worker.moveToThread(self.thread)

                # connect signals to slots
                self.worker.addMessage.connect(self._message_from_worker)
                self.worker.addError.connect(self._error_from_worker)
                self.worker.progress.connect(self._progress_from_worker)
                self.worker.total.connect(self._total_from_worker)
                self.worker.api.connect(self._report_api_usage)

                self.thread.started.connect(self.worker.run)
                self.worker.finished.connect(self.thread.quit)
                self.worker.finished.connect(self.worker.deleteLater)
                self.thread.finished.connect(self.thread.deleteLater)

                # start thread and run worker
                self.thread.start()

                # enable button after thread finishes; set download not in progress
                def worker_finished(placesData): 
                    self.startButton.setEnabled(True)    
                    self.stopButton.setEnabled(False)
                    self.isDownloadInProgress = False
                    self.progressBar.setValue(self.progressBar.maximum())  

                    if type(placesData) == pd.DataFrame and len(placesData) > 0:    
                            self.placesData = placesData
                            self._draw_layers(latitude, longitude, radius)
                    
                self.worker.finished.connect(worker_finished)
            else:
                QMessageBox.warning(self, "Error", "Can not download without appropriate data!")

    def _draw_layers(self, clat, clong, radius):
        self.logBox.append('drawing vector layers...')

        # create boundary layer
        self.boundaryLayer = QgsVectorLayer("Point?crs=epsg:4326", "places boundary", "memory")
        self.boundaryProvider = self.boundaryLayer.dataProvider()
        self.boundaryLayer.startEditing()

        # define symbol to be a boundary
        symbol = QgsMarkerSymbol.createSimple({
            'name': 'circle', 
            'color': '255, 255, 255, 0',
            'size': str(2 * radius * 1_000),
            'size_unit': 'RenderMetersInMapUnits',
            'outline_color': '35,35,35,255', 
            'outline_style': 'solid', 
            'outline_width': '10',
            'outline_width_unit': 'RenderMetersInMapUnits'
        })
        
        self.boundaryLayer.renderer().setSymbol(symbol)

        # draw circular boundary
        boundary = QgsFeature()
        boundary.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(clong, clat)))
        self.boundaryProvider.addFeatures([boundary])

        self.boundaryLayer.commitChanges()
        QgsProject.instance().addMapLayer(self.boundaryLayer)

        # create marker layer
        self.markerLayer = QgsVectorLayer("Point?crs=epsg:4326", "places markers", "memory")
        self.markerProvider = self.markerLayer.dataProvider()
        self.markerLayer.startEditing()

        self.markerProvider.addAttributes([
            QgsField('name', QVariant.String),
            QgsField('latitude', QVariant.Double),
            QgsField('longitude', QVariant.Double),
            QgsField('place_id', QVariant.String),
            QgsField('types', QVariant.List),
            QgsField('reviews', QVariant.Hash)
        ])

        self.logBox.append(f"adding {len(self.placesData)} features")

        for _, row in self.placesData.iterrows():
            marker = QgsFeature()
            marker.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(row['long'], row['lat'])))
            marker.setAttributes([
                row['name'],
                float(row['lat']),
                float(row['long']),
                row['place_id'],
                row['types'],
                row['data']['reviews']
            ])
            self.markerProvider.addFeatures([marker])

        self.logBox.append(f"added {len(self.placesData)} features")

        self.markerLayer.commitChanges()
        QgsProject.instance().addMapLayer(self.markerLayer)

        # add selection handler
        self.markerLayer.selectionChanged.connect(self._handle_feature_selection)
        self.webViews = []

    def _open_web_view(self, name, lat, long, place_id, types, reviews):
        webView = QWebView()
        self.webViews.append(webView)

        self.logBox.append(f"loading {name} ...")

    def _handle_feature_selection(self, selFeatures):
        selFeatures = self.markerLayer.selectedFeatures()
        if len(selFeatures) > 0:
            for feature in selFeatures:
                name, lat, long, place_id, types, reviews = feature.attributes()
                # draw popup on web view or use native qt dialog
                self._open_web_view(name, lat, long, place_id, types, reviews)
        
    def _stop_download_thread(self):
        self.worker.stop()

    def _message_from_worker(self, message):
        self.logBox.append(message)

    def _error_from_worker(self, message):
        QMessageBox.warning(self, "Error", message)

    def _progress_from_worker(self, progress):
        self.progressBar.setValue(progress)

    def _total_from_worker(self, total):
        self.progressBar.setMaximum(int(total))

    def _show_api_usage(self):
        if os.path.exists(self.usageFilePath):
            f = open(self.usageFilePath, 'r')
            
            for line in f.readlines():
                key, val = line.strip("\n").split("=")
                if key != 'LASTDATE':
                    self.api_report_map[key].setText(val)

    def _report_api_usage(self, usage):
        if os.path.exists(self.usageFilePath):
            f = open(self.usageFilePath, 'r')
            l = []

            for line in f.readlines():
                key, val = line.strip("\n").split("=")
                if key != 'LASTDATE':
                    usage[key] += int(val)
                    l.append(f"{key}={usage[key]}")
                else:
                    currMonth = datetime.now().month
                    if currMonth != val:
                        # reset api usage data
                        l = ["NEARBY=0", "REVIEWS=0", "PHOTOS=0"]
                        for key in usage:
                            usage[key] = 0
                    
            l.append(f"LASTDATE={datetime.now().month}")
            f.close()

            f = open(self.usageFilePath, 'w')
            f.truncate(0)
            f.write('\n'.join(l))
            f.close()
        else:
            f = open(self.usageFilePath, 'w')
            l = []

            for key, val in usage.items():
                l.append(f"{key}={val}")

            l.append(f"LASTDATE={datetime.now().month}")

            f.write('\n'.join(l))
            f.close()
        
        self._show_api_usage()
            
            


class Worker(QObject):
    finished = pyqtSignal(pd.DataFrame)
    progress = pyqtSignal(int)
    addMessage = pyqtSignal(str)
    addError = pyqtSignal(str)
    total = pyqtSignal(int)
    api = pyqtSignal(dict)

    def __init__(self, latitude, longitude, radius, xlsxFilePath, gapiKey, keyword, outputDirName, saveImages, limitEntries):
        QObject.__init__(self)
        self.lat = latitude
        self.long = longitude
        self.radius = radius * 1000 # convert to metres
        self.xlsxFilePath = xlsxFilePath
        self.gapiKey = gapiKey
        self.keyword = keyword
        self.outputDirName = outputDirName
        self.saveImages = saveImages
        self.limitEntries = limitEntries

        self.running = None
        self.placeDownloadCount = 0
        self.imageDownloadCount = 0

        self.imageBaseURL = "https://maps.googleapis.com/maps/api/place/photo"

        self.nearbySearchUsage = 0
        self.placeDetailsUsage = 0
        self.placePhotoUsage   = 0

    def stop(self):
        self.running = False

    def _search_places(self):
        # search for places within radius using the nearby places API
        url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        
        # TODO: sort out the keyword issue
        params = {
            # "keyword"   : self.keyword,
            "location"  : f"{self.lat},{self.long}",
            "radius"    : str(self.radius),
            "key"       : self.gapiKey
        }
        
        self.addMessage.emit(f"searching for nearby places...")
        results = []
        
        while len(results) <= self.limitEntries:
            res = requests.get(url, params=params)
            data = res.json()

            self.nearbySearchUsage += 1

            if data['status'] == 'OK':
                results = results + data['results']
            else:
                self.addError.emit(f"Error fetching nearby places. {data['error_message']}")
                break

            if 'next_page_token' in data and data['next_page_token'] != '':
                params['pagetoken'] = data['next_page_token']
            else:
                # no more pages
                break

            # wait for next page token to be valid
            time.sleep(NPT_VALIDITY_DELAY)

        return results[:self.limitEntries]

    def _get_reviews(self, place_id):
        self.placeDownloadCount += 1
        self.progress.emit(int(METADATA_DOWNLOAD_PROGRESS + (100 - METADATA_DOWNLOAD_PROGRESS - IMAGE_DOWNLOAD_PROGRESS) * self.placeDownloadCount / self.countPlaces))

        if not self.running:
            self.halt_error()

        # get reviews from place_id
        url = "https://maps.googleapis.com/maps/api/place/details/json"
        fields = ['review', 'photo']
        params = {
            'fields'    : ','.join(fields),
            'place_id'  : place_id,
            'key'       : self.gapiKey
        }
        data = requests.get(url, params=params).json()
        self.placeDetailsUsage += 1

        if data['status'] == 'OK':
            res = {}
            if 'reviews' in data['result']:
                self.addMessage.emit(f"Fetched reviews for place: {place_id}")
                res['reviews'] = data['result']['reviews']
            else:
                self.addMessage.emit(f"No reviews found for place: {place_id}")
                return np.nan
            if 'photos' in data['result']:
                self.addMessage.emit(f"Fetched photos for place: {place_id}")
                res['photos'] = data['result']['photos']
            else:
                self.addMessage.emit(f"No photos found for place: {place_id}")
            
            return res
        else:
            self.addMessage.emit(f"Error fetching review and/or photos for place: {place_id}. {data['error_message']}")
            return np.nan

    def _get_photos(self, place_id, photos):
        index = 1
        for photo in photos:   
            self.imageDownloadCount += 1
            self.progress.emit(int((100 - IMAGE_DOWNLOAD_PROGRESS) + IMAGE_DOWNLOAD_PROGRESS * self.imageDownloadCount / self.countImages))
            if not self.running:
                self.halt_error()
                return

            filename = f"{place_id}_{index}.jpg"
            filepath = os.path.join(self.outputDirName, filename)
            params = {
                "photoreference": photo['photo_reference'],
                "sensor": "false",
                "maxheight": photo['height'],
                "maxwidth": photo['width'],
                "key": self.gapiKey
            }
            r = requests.get(self.imageBaseURL, params=params, stream=True)
            self.placePhotoUsage += 1

            if r.status_code == 200:
                try:
                    with open(filepath, 'wb') as f:
                        for chunk in r.iter_content(CHUNK_SIZE):
                            f.write(chunk)
                except:
                    self.addMessage.emit(f"could not write file {filename}")
                else:
                    self.addMessage.emit(f"saved file {filename}")
            else:
                self.addMessage.emit(f"could not download file {filename}")

            index += 1

    def halt_error(self):
        self.addMessage.emit("worker halted forcefully")
        self.api.emit({
            "NEARBY": self.nearbySearchUsage,
            "REVIEWS": self.placeDetailsUsage,
            "PHOTOS": self.placePhotoUsage
        })
        self.finished.emit(pd.DataFrame())

    def run(self):
        self.placeDownloadCount = 0
        self.running = True
        self.total.emit(100)

        # download nearby places
        places = self._search_places()
        self.countPlaces = len(places)

        self.progress.emit(METADATA_DOWNLOAD_PROGRESS)

        if not self.running:
            self.halt_error()

        if places == None:
            self.addMessage.emit("No places fetched. Aborting...")
            self.api.emit({
                "NEARBY": self.nearbySearchUsage,
                "REVIEWS": self.placeDetailsUsage,
                "PHOTOS": self.placePhotoUsage
            })
            self.finished.emit(pd.DataFrame())
            return
        else:
            self.addMessage.emit(f"{len(places)} places found")

        placeData = []
        for place in places:
            row = []
            row.append(place['geometry']['location']['lat'])
            row.append(place['geometry']['location']['lng'])
            row.append(place['name'])
            row.append(place['place_id'])
            row.append(place['types'])
            placeData.append(row)

        placeData = pd.DataFrame(placeData, columns=['lat', 'long', 'name', 'place_id', 'types'])
        
        # get data from place id
        placeData['data'] = placeData['place_id'].apply(self._get_reviews)

        # drop rows with no data
        placeData = placeData.dropna(subset=['data'])

        if not self.running:
            self.halt_error()

        # FLUSH DATA TO XLSX FILE
        self.addMessage.emit(f"flushing {len(placeData)} places to excel workbook...")
        workbook = xlsxwriter.Workbook(self.xlsxFilePath)

        bold = workbook.add_format({'bold': True})

        # FORMATTED WORKSHEET
        worksheet_formatted = workbook.add_worksheet('reviews-formatted')
        currRow = 0
        worksheet_formatted.write(currRow, 0, "No.", bold)
        for index, colName in enumerate(list(placeData.columns[:5]) + ["author", "comment", "timestamp"]):
            worksheet_formatted.write(currRow, index+1, colName, bold)
        
        currRow = 2

        for index, place in placeData.iterrows():
            if not self.running:
                self.halt_error()
            reviews = place['data']['reviews']
            numReviews = len(reviews)
            col = 'A'
            if numReviews > 1:
                worksheet_formatted.merge_range(f"{col}{currRow}:{col}{currRow + numReviews - 1}", index)
            col = chr(ord(col) + 1)

            for data in place[placeData.columns[:4]]:
                if numReviews > 1:
                    worksheet_formatted.merge_range(f"{col}{currRow}:{col}{currRow + numReviews - 1}", data)
                col = chr(ord(col) + 1)

            if numReviews > 1:
                worksheet_formatted.merge_range(f"{col}{currRow}:{col}{currRow + numReviews - 1}", ', '.join(place['types']))
            col = chr(ord(col) + 1)

            for review in reviews:
                worksheet_formatted.write(currRow - 1, 6, review['author_name'])
                worksheet_formatted.write(currRow - 1, 7, review['text'])
                worksheet_formatted.write(currRow - 1, 8, datetime.utcfromtimestamp(review['time']).strftime('%A, %d %B, %Y'))
                currRow += 1

        # UNFORMATTED WORKSHEET
        worksheet_unformatted = workbook.add_worksheet('reviews-unformatted')
        currRow = 0
        worksheet_unformatted.write(currRow, 0, "No.", bold)
        for index, colName in enumerate(list(placeData.columns[:5]) + ["author", "comment", "timestamp"]):
            worksheet_unformatted.write(currRow, index+1, colName, bold)
        
        currRow = 1

        for index, place in placeData.iterrows():
            if not self.running:
                self.halt_error()
            reviews = place['data']['reviews']
           
            for review in reviews:
                worksheet_unformatted.write(currRow, 0, index)
                worksheet_unformatted.write(currRow, 1, place['lat'])
                worksheet_unformatted.write(currRow, 2, place['long'])
                worksheet_unformatted.write(currRow, 3, place['name'])
                worksheet_unformatted.write(currRow, 4, place['place_id'])
                worksheet_unformatted.write(currRow, 5, ', '.join(place['types']))
                worksheet_unformatted.write(currRow, 6, review['author_name'])
                worksheet_unformatted.write(currRow, 7, review['text'])
                worksheet_unformatted.write(currRow, 8, datetime.utcfromtimestamp(review['time']).strftime('%A, %d %B, %Y'))
                currRow += 1

        # widen columns to improve readability
        for col, width in XLSX_COL_WIDTHS.items():
            worksheet_formatted.set_column(f"{col}:{col}", width)
            worksheet_unformatted.set_column(f"{col}:{col}", width)

        try:
            workbook.close()
            self.addMessage.emit("saved data to excel file")
        except Exception as ex:
            self.addError.emit(f"Error writing to excel file. {ex}")

        # download all images
        if self.saveImages:   
            # count number of images
            self.countImages = 0
            for _, place in placeData.iterrows():
                if 'photos' in place['data']:
                    self.countImages += len(place['data']['photos'])

            self.addMessage.emit(f"downloading {self.countImages} images...")
            for _, place in placeData.iterrows():
                if 'photos' in place['data']:
                    self._get_photos(place['place_id'], place['data']['photos'])
            self.addMessage.emit(f"downloaded all {self.countImages} images")
        else:
            self.progress.emit(100)

        self.api.emit({
            "NEARBY": self.nearbySearchUsage,
            "REVIEWS": self.placeDetailsUsage,
            "PHOTOS": self.placePhotoUsage
        })
        self.finished.emit(placeData)
        return
