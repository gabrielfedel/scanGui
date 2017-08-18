#!/usr/bin/env python3.4
import json
from PyQt5.QtWidgets import QTableWidgetItem


class qtArgs():
    '''Load and save arguments from a qt window to json files
    The idea is to provide a generic way to save information from
    diferents widgets and a way to recover this information correctly'''

    def __init__(self, window):
        # args format is {'name' : {'load' : 'methodloadname',
        # 'update': 'methodupdatename', 'value' : 'value'}}
        self.args = {}
        self.window = window

    # TODO change this to a method that get the data from window automatically
    def saveArg(self, name, value = None , load = None, update = None, qtw = False):
        '''qtw: indicate if the arg is a QTableWidget '''

        # When is a QTableWidget, get all data
        if qtw :
            value = []
            widget = getattr(self.window, name)

            for row in range(0, widget.rowCount()):
                values_row = []
                # store each row item on values_row
                for column in range(0, widget.columnCount()):
                    values_row.append(widget.item(row,column).text())

                # store the row on values
                value.append(values_row)

        self.args.update({name: {'value': value, 'load': load,
                         'update': update}})

    def storeArgs(self, filename):
        with open(filename, 'w') as f:
            json.dump(self.args, f)

    def loadArgs(self, filename):
        # load args from json file
        with open(filename, 'r') as f:
            self.args = json.loads(f.read())

        # for each arg, load the widget, then the update function and
        # put the value
        for name, info in self.args.items():
            widget = getattr(self.window, name)
            value = info['value']

            # its not a QTableWidget
            if type(value) is not list:
                fsave = getattr(widget, info['update'])
                fsave(value)
            # is a QTableWidget
            else:
                # set the numver of rows
                widget.setRowCount(len(value))

                # save each item on each item
                for idx_row, row in enumerate(value):
                    for idx_col, item in enumerate(row):
                        # need to convert value to QTableWidgetItem
                        item = QTableWidgetItem(value[idx_row][idx_col])
                        widget.setItem(idx_row, idx_col,item)
