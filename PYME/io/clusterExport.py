# -*- coding: utf-8 -*-
"""
Created on Sun May 22 17:13:51 2016

@author: david
"""

from PYME.Acquire import HTTPSpooler, MetaDataHandler

import dispatch

class ImageFrameSource(object):
    def __init__(self):
        #self.image = image
        
        self.onFrame = dispatch.Signal(['frameData'])
        self.spoolProgress = dispatch.Signal(['percent'])
        
    def spoolImageFromFile(self, filename):
        '''Load an image file and then spool'''
        from PYME.io import image
        
        self.spoolImage(image.ImageStack(filename).data)
        
    def spoolData(self, data):
        '''Extract frames from a data source.
        
        Parameters
        ----------
        
        data : PYME.io.DataSources.DataSource object
            the data source. Needs to implement the getNumSlices() and getSlice()
            methods.
        '''
        nFrames = data.getNumSlices()
        for i in range(nFrames):
            self.onFrame.send(self, frameData=data.getSlice(i))
            if (i % 10) == 0:
                self.spoolProgress.send(self, percent=float(i)/nFrames)
                print('Spooling %d of %d frames' % (i, nFrames))
            
          

class MDSource(object):
    '''Spoof a metadata source for the spooler'''
    def __init__(self, mdh):
        self.mdh = mdh

    def __call__(self, md_to_fill):
        md_to_fill.copyEntriesFrom(self.mdh)
         
         
def ExportImageToCluster(image, filename, progCallback=None):
    '''Exports the given image to a file on the cluster
    
    Parameters
    ----------
    
    image : PYME.io.image.ImageStack object
        the source image
    filename : string
        the filename on the cluster
        
    '''
    
    #create virtual frame and metadata sources
    imgSource = ImageFrameSource()
    mds = MDSource(image.mdh)
    MetaDataHandler.provideStartMetadata.append(mds)
    
    if not progCallback is None:
        imgSource.spoolProgress.connect(progCallback)
    
    #queueName = getRelFilename(self.dirname + filename + '.h5')
    
    #generate the spooler
    spooler = HTTPSpooler.Spooler(filename, imgSource.onFrame, frameShape = image.data.shape[:2])
    
    #spool our data    
    spooler.StartSpool()
    imgSource.spoolData(image.data)
    spooler.FlushBuffer()
    spooler.StopSpool()
    
    #remove the metadata generator
    MetaDataHandler.provideStartMetadata.remove(mds)


SERIES_PATTERN = '%(day)d_%(month)d_series_%(counter)'

def _getFilenameSuggestion(dirname='', seriesname = SERIES_PATTERN):
    from PYME.io.FileUtils import nameUtils
    from PYME.ParallelTasks import clusterIO
    import os
    
    if dirname == '':   
        dirname = nameUtils.genClusterDataFilepath()
    else:
        dirname = dirname.split(nameUtils.getUsername())[-1]
        
        dir_parts = dirname.split(os.path.sep)
        if len(dirname) < 1 or len(dir_parts) > 3:
            #path is either too complex, or too easy - revert to default
            dirname = nameUtils.genClusterDataFilepath()
        else:
            dirname = nameUtils.getUsername() + '/'.join(dir_parts)
    
    #dirname = defDir % nameUtils.dateDict
    seriesStub = dirname + '/' + seriesname % nameUtils.dateDict

    seriesCounter = 0
    seriesName = seriesStub % {'counter' : nameUtils.numToAlpha(seriesCounter)}
        
    #try to find the next available serie name
    while clusterIO.exists(seriesName + '/'):
        seriesCounter +=1
        
        if '%(counter)' in seriesName:
            seriesName = seriesStub % {'counter' : nameUtils.numToAlpha(seriesCounter)}
        else:
            seriesName = seriesStub + '_' + nameUtils.numToAlpha(seriesCounter)
            
    return seriesName

def SaveImageToCluster(image):
    import os
    import wx
    
    if not image.filename is None:
        dirname, seriesname = os.path.split(image.filename)
        
        seriesName = _getFilenameSuggestion(dirname, seriesname)
    else:
        seriesName = _getFilenameSuggestion()
        
    ted = wx.TextEntryDialog(None, 'Cluster filename:', 'Save file to cluster', seriesName)
    
    if ted.ShowModal() == wx.ID_OK:
        #pd = wx.ProgressDialog()
        ExportImageToCluster(image, seriesName)
        
    ted.Destroy()
        