from epics import PV
from py4syn.epics.StandardDevice import StandardDevice
from py4syn.epics.ICountable import ICountable

class CountablePV(StandardDevice, ICountable):
        '''Adds fake ICountable support for generic PV'''
        def __init__(self, pvName, mnemonic):
                StandardDevice.__init__(self, mnemonic)
                self.pvName = pvName
                self.pv = PV(pvName)
        
        def getValue(self, **kwargs):
                return self.pv.get()

        def setCountTime(self, t):
                pass
        
        def setPresetValue(self, channel, val):
                pass
        
        def startCount(self):
                pass
        
        def stopCount(self):
                pass
        
        def canMonitor(self):
                return False
        
        def canStopCount(self):
                return True
        
        def isCounting(self):
                return False
        
        def wait(self):
                pass

