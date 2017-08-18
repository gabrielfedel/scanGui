#!/usr/bin/env python3
"""An interface for scan on pyqt """
import sys
import os
import time
from datetime import datetime, timedelta
from glob import glob

from gui.window import Ui_MainWindow
from scan import ScanMotors

from PyQt5 import QtWidgets
from PyQt5.QtCore import QThread, QObject, pyqtSlot, pyqtSignal
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtGui import QTextCursor
from py4syn.utils.scan import scanDataToLine, scanHeader, setPlotGraph, getScanData
from PyQtArgs.qtArgs import qtArgs

SCAN_UTILS = "/usr/local/scripts/scan-utils/*.*.yml"
FACTOR_TIME = 10
# dictionary with the time of pauses
PAUSES = {'p1': {'H': 8, 'M':0}, 'p2': {'H': 19, 'M': 0}}
# how minutes before beam end will pause the scan 
# (when time acquistion < MINUTESTOEND
MINUTESTOEND = 3

def checkPauseTime(pauses, secsToEnd):
    """Verify if time to pause is less then secsToEnd seconds
    If time to pause is less then secToEnd seconds, returns time
    else returns false"""
    for i in range(1, len(pauses)+1):
        tnow = datetime.now()
        tn = timedelta(
                seconds=tnow.second,
                minutes=tnow.minute,
                hours=tnow.hour)
        tp = timedelta(
                minutes=pauses['p%d' % (i)]['M'],
                hours=pauses['p%d' % (i)]['H'])
        if(tp > tn and tp-tn < timedelta(seconds=secsToEnd)):
            return (tp-tn)
    return False


class ScanMotorsT(ScanMotors, QThread):
    """A thread for scan motors
    This class encapsulate scan in a thread and include functions
    to work with a GUI"""
    writeSignal = pyqtSignal(str)
    blEndSignal = pyqtSignal()

    def __init__(self, arg, writeSlot, blEndSlot):
        """ 
        writeSlot: slot to write messages
        blEndSLot: slot to run beamline end
        """
        ScanMotors.__init__(self, args = arg)
        QThread.__init__(self)
        self.writeSignal.connect(writeSlot)
        self.blEndSignal.connect(blEndSlot)
        setPlotGraph(False)

        # for cases with only 1 motor
        if self.time is None:
            self.time = self.acquisitionTime[0] 
        

        if self.time < 60*MINUTESTOEND:
            self.secsToEnd = 60*MINUTESTOEND
        else:
            self.secsToEnd = self.time*1.1

    def run(self):
        ScanMotors.runScan(self)

    def preScanCallback(self, counters, rows, cols, **kwargs):
        self.writeSignal.emit("Start time: %s \n" % (str(datetime.now())) )
        self.writeSignal.emit(scanHeader()+ "\n")
        ScanMotors.preScanCallback(self, counters, rows, cols, **kwargs)

    def prePointCallback(self, **kwargs):
        """Verify if is next to pause time"""
        scanData = self._getScanData()
        #if is running
        if scanData is not None:
            if checkPauseTime(PAUSES,self.secsToEnd):
                self.pause()
                # emit signal to send beam stop message
                self.blEndSignal.emit()

    def postPointCallback(self, countersConf, counters, configuration, constants, **kwargs):
        self.writeSignal.emit(scanDataToLine(format="4") + "\n")
        ScanMotors.postPointCallback(self, countersConf, counters, configuration, constants, **kwargs)

    def postScanCallback(self, counters, **kwargs):
        self.writeSignal.emit("End time: %s \n" % ( str(datetime.now())) )
        ScanMotors.postScanCallback(self, counters, **kwargs)

    def _getScanData(self):
        s = getScanData()
        if s is not None:
            return s['scan_object']
        else:
            return None

    #TODO: implement a better way interrupt, pause and resume
    def interrupt(self):
        scanData = self._getScanData()
        if scanData is not None:
            scanData.interrupt()

    def pause(self):
        scanData = self._getScanData()
        if scanData is not None:
            scanData.pause()

    def resume(self):
        scanData = self._getScanData()
        if scanData is not None:
            scanData.resume()


class ScanGui(QObject):
    def __init__(self, ui):
        super().__init__()

        self.ui = ui

        # connection all signals to slots
        self.ui.btnStart.released.connect(self.start)
        self.ui.btnStart.released.connect(self.toggleStartStop)
        self.ui.btnStop.released.connect(self.stop)
        self.ui.btnPause.released.connect(self.pause)
        self.ui.btnResume.released.connect(self.resume)
        self.ui.btnAddLine.released.connect(self.addLine)
        self.ui.btnPath.released.connect(self.chooseDir)
        self.ui.btnSave.released.connect(self.saveArgs)
        self.ui.btnLoad.released.connect(self.loadArgs)
        self.ui.twRuns.cellChanged.connect(self.timeExpected)
        self.ui.cmbMotor2.currentIndexChanged.connect(self.timeExpected)

        self.sc = None

        # list all counters
        counFiles = glob(SCAN_UTILS)
        self.ui.cmbCounter.addItems(sorted([c.split(".yml")[0].split(".")[-1] for c in counFiles]))
        # set default counter to dxp (if exist)
        pos = self.ui.cmbCounter.findText("dxp")
        if pos > -1:
            self.ui.cmbCounter.setCurrentIndex(pos)
        else:
            self.ui.cmbCounter.setCurrentIndex(0)

        self.ui.cmbMotor1.setCurrentIndex(2)

        # initial time expected calc
        self.timeExpected()


    def callScan(self):
        """Call scan script"""

        fmt = '%Y%m%d%H%M%S'  # timestamp format: year month day hour minute
        experiment = 'scan_points'  +\
                    datetime.fromtimestamp(time.time()).strftime(fmt)
        filePath = self.ui.lePath.text() + '/' + experiment
        if not os.path.exists(filePath):
            try:
                os.makedirs(filePath)

                self.arguments = {'motor': None, 'initial': None, 'final': None,
                        'stepOrCount': None, 'steps':  None, 'acquisitionTime': None,
                        'relative': None,'sync': None, 'output' : None, 'sleep': 0.0,
                        'message': None, 'optimum': None, 'time': None, 'count': 1}

                # Colect the arguments from window
                self.arguments['output'] = filePath + '/data' 
                self.arguments['configuration'] = self.ui.cmbCounter.currentText()

                row = self.nextRun

                try:
                    # only 1 motor
                    if self.ui.cmbMotor2.currentText() == "" :
                        m1Init = self.ui.twRuns.item(row, 0).text()
                        m1End = self.ui.twRuns.item(row, 1).text()
                        m1Step = self.ui.twRuns.item(row, 2).text()
                        cntTime = self.ui.twRuns.item(row, 6).text()

                        # verify if all fields has data
                        if (m1Init != "" and m1End != "" and m1Step and
                           cntTime != ""):
                            #TODO: fix to use 1 motor
                            self.arguments['initial'] = [float(m1Init)]
                            self.arguments['final'] = [float(m1End)]
                            self.arguments['stepOrCount'] = [float(m1Step)]
                            self.arguments['acquisitionTime'] = [float(cntTime)/1000] # in seconds
                            self.arguments['motor'] = self.ui.cmbMotor1.currentText()

                            self.ui.tbOutput.append("===== Run %d ===== \n" %
                                                    (self.nextRun + 1))


                            self.sc = ScanMotorsT(self.arguments, self.appendText,self.beamlineEnd)
                            self.sc.start()
                            self.sc.finished.connect(self.finish)

                    else:
                        m1Init = self.ui.twRuns.item(row, 0).text()
                        m1End = self.ui.twRuns.item(row, 1).text()
                        m1Step = self.ui.twRuns.item(row, 2).text()
                        m2Init = self.ui.twRuns.item(row, 3).text()
                        m2End = self.ui.twRuns.item(row, 4).text()
                        m2Step = self.ui.twRuns.item(row, 5).text()
                        cntTime = self.ui.twRuns.item(row, 6).text()

                        # verify if all fields has data
                        if (m1Init != "" and m1End != "" and m1Step != "" and
                           m2Init != "" and m2End != "" and m2Step != "" and
                           cntTime != ""):
                            #TODO: fix to use 1 motor
                            self.arguments['initial'] = [float(m1Init), float(m2Init)]
                            self.arguments['final'] = [float(m1End), float(m2End)]
                            self.arguments['steps'] = [float(m1Step),float(m2Step)]
                            self.arguments['time'] = float(cntTime)/1000 # in seconds

                            self.arguments['motor'] = [self.ui.cmbMotor1.currentText(),self.ui.cmbMotor2.currentText()]

                            self.ui.tbOutput.append("===== Run %d ===== \n" %
                                                    (self.nextRun + 1))


                            self.sc = ScanMotorsT(self.arguments, self.appendText, self.beamlineEnd)
                            self.sc.start()
                            self.sc.finished.connect(self.finish)

                except AttributeError:
                    # when some field is empty
                    self.toggleStartStop()
                    pass

                # there are more runs increase nextRow, else it receives 0
                if self.ui.twRuns.rowCount() > row + 1:
                    self.nextRun += 1
                else:
                    self.nextRun = 0


            except PermissionError:
                self.showDialog("Directory Error", "Permission Error on Directory")
        else:
            self.showDialog("Directory Error", "Directory already exists")

    # slots

    @pyqtSlot()
    def toggleStartStop(self):
        self.ui.btnStop.setEnabled(not self.ui.btnStop.isEnabled())
        self.ui.btnStart.setEnabled(not self.ui.btnStart.isEnabled())
        self.ui.btnPause.setEnabled(not self.ui.btnPause.isEnabled())
        self.ui.btnResume.setEnabled(not self.ui.btnResume.isEnabled())

    @pyqtSlot()
    def start(self):
        self.nextRun = 0
        self.ui.tbOutput.clear()
        self.callScan()

    @pyqtSlot()
    def stop(self):
        self.sc.interrupt()

    @pyqtSlot()
    def pause(self):
        self.sc.pause()

    @pyqtSlot()
    def resume(self):
        self.sc.resume()

    @pyqtSlot()
    def finish(self):
        #there are more runs
        if self.nextRun != 0:
            self.callScan()
        else:
            self.toggleStartStop()


    @pyqtSlot(str, str)
    def showDialog(self, title, text):
        """Show a simple message dialog"""
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Information)
        msg.setText(text)
        msg.setWindowTitle(title)
        msg.exec_()

    @pyqtSlot(str)
    def appendText(self, text):
        self.ui.tbOutput.moveCursor(QTextCursor.End)
        self.ui.tbOutput.insertPlainText( text )

    @pyqtSlot()
    def addLine(self):
        """Add a line to the runs """
        rows = self.ui.twRuns.rowCount()
        self.ui.twRuns.setRowCount(rows + 1)

    @pyqtSlot()
    def chooseDir(self):
        '''Open a Choose Directory Dialog and save result on lePath'''
        self.ui.lePath.setText(
            QtWidgets.QFileDialog.getExistingDirectory(ui.btnPath))

    @pyqtSlot()
    def saveArgs(self):
        # user choose where save
        filename = QtWidgets.QFileDialog.getSaveFileName(
            ui.btnSave, 'Choose a file to save')[0]

        if filename != '':
            # for args outside QtableWidget
            pqa = qtArgs(self.ui)

            # saving all widgets
            # on combo, we save the index
            pqa.saveArg('cmbCounter', self.ui.cmbCounter.currentIndex(),
                        'currentIndex', 'setCurrentIndex')
            pqa.saveArg('lePath', self.ui.lePath.text(), 'text', 'setText')
            pqa.saveArg('cmbMotor1', self.ui.cmbMotor1.currentIndex(),
                        'currentIndex', 'setCurrentIndex')
            pqa.saveArg('cmbMotor2', self.ui.cmbMotor2.currentIndex(),
                        'currentIndex', 'setCurrentIndex')

            # save QTableWidget data
            pqa.saveArg('twRuns', qtw=True)

            # store on file
            pqa.storeArgs(filename)

    @pyqtSlot()
    def loadArgs(self):
        # user choose what file load
        filename = QtWidgets.QFileDialog.getOpenFileName(
            ui.btnSave, 'Choose a file to load')[0]

        if filename != '':
            pqa = qtArgs(self.ui)
            pqa.loadArgs(filename)

    @pyqtSlot()
    def timeExpected(self):
        '''Calculate time expected'''
        try:
            totalTime = 0

            for row in range(0, self.ui.twRuns.rowCount()):
                cntTime = int(self.ui.twRuns.item(row, 6).text())
                initial = float(self.ui.twRuns.item(row, 0).text())
                final = float(self.ui.twRuns.item(row, 1).text())
                stepSize = float(self.ui.twRuns.item(row, 2).text())
                # only 1 motor
                if self.ui.cmbMotor2.currentText() == "":
                    lines = 1
                # 2 motors 
                else:
                    initialSM = float(self.ui.twRuns.item(row, 3).text())
                    finalSM = float(self.ui.twRuns.item(row, 4).text())
                    stepSM = float(self.ui.twRuns.item(row, 5).text())
                    lines = round(abs((finalSM - initialSM)/stepSM)) + 1

                points = round(abs((final - initial)/stepSize)) + 1
                movTime = float((cntTime + FACTOR_TIME) / 1000)
                totalTime += movTime*points*lines

            self.ui.lblEstTime.setText(str(timedelta(seconds=int(totalTime))))
        except ValueError:
            self.ui.lblEstTime.setText("Invalid value")
        except AttributeError:
            # when some item is empty, just show totalTime
            self.ui.lblEstTime.setText(str(timedelta(seconds=int(totalTime))))

    @pyqtSlot()
    def beamlineEnd(self):
        """Process the pause when beamline ends"""
        self.showDialog("Scan Paused", "We are close to the end of beam. Scan is paused. Please be sure the beam is back and the shutter is already open before pressing <OK> to resume the present scan")
        # after press OK, resume the scan
        self.sc.resume()

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    MainWindow = QtWidgets.QMainWindow()
    ui = Ui_MainWindow()
    ui.setupUi(MainWindow)
    MainWindow.show()

    sg = ScanGui(ui)

    sys.exit(app.exec_())
#a = scanMotorsT()
#a.start()
