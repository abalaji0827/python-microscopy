import numpy as np
from PYME.IO import MetaDataHandler

def isnumber(s):
    try:
        float(s)
        return True
    except:
        return False


class CSVSMLMReader(object):

    csv_flavours = {
        'elyra' : {
            'idnames' : ['Index','First Frame'], # column names that identify this flavour
            'comment_char' : '#', # it does not really work to use these the comment char to ignore lines with missing data
            'delimiter' : '\t',
            'column_name_mappings' : {  # a dictionary of name substitutions
                'Position X [nm]': 'x',
                'Position Y [nm]': 'y',
                'Number Photons': 'nPhotons',
                'First Frame': 't',
                'Chi square': 'nchi2',
                'PSF half width [nm]': 'sigfwhm', # this one needs a mapping to sig really
                'Precision [nm]': 'error_x'
            },
            'must_ignore_errors' : True, # these files bomb unless we tell np.genfromtxt to ignore errors
            # 'column_translations' : {}, # a dict to populate a MappingFilter (To be confirmed)
        },
        'simple' : { # placeholder for more vanilla QuickPALM/simple ThunderSTORM
            'idnames' : ['frame','int'],
            'delimiter' : ',',
            'comment_char' : '#', # it does not really work to use these the comment char to ignore lines with missing data
            'column_name_mappings' : {  # a dictionary of name substitutions
                'frame' : 't',
                'int' : 'nPhotons',
                'X' : 'x',
                'Y' : 'y',
                'Z' : 'z',
            },
        },
        'thunderstorm' : {
            'idnames' : ['frame','x [nm]'],
            'comment_char' : '#', # it does not really work to use these the comment char to ignore lines with missing data
            'delimiter' : ',',
            'column_name_mappings' : {  # a dictionary of name substitutions
                'frame' : 't',
                'x [nm]' : 'x',
                'y [nm]' : 'y',
                'z [nm]' : 'z',
                'uncertainty_xy [nm]' : 'error_x',
                'uncertainty_z [nm]' : 'error_z',
                'intensity [photon]' : 'nPhotons',
                'sigma1 [nm]' : 'sigx',
                'sigma2 [nm]' : 'sigy',
                'sigma [nm]' : 'sig',
            },
        },
    }

    requiredNames = {'x':'x position [nm]',
                     'y':'y position [nm]'}

    def __init__(self, file):
        self.filename = file # do we allow file to be something other than a name?
        self.flavour = 'default'

    def flavour_value_or_default(self,key,default=None):
        if not hasattr(self,'flavour'):
            raise RuntimeError('flavour not yet defined!')
        if self.flavour == 'default':
            return default
        if key in self.csv_flavours[self.flavour]:
            return self.csv_flavours[self.flavour][key]
        else:
            return default


    def parse_header_csv(self):
        n = 0
        commentLines = []
        dataLines = []
        headerNameLines = []

        def is_header_candidate(line, delims):
            guessedDelim = guess_delim(line,delims)
            if guessedDelim is not None:
                return not isnumber(line.split(guessedDelim)[0])
            else:
                return False

        def guess_delim(line,delims):
            maxItems = 1
            guessedDelim = None
            
            for delim in delims:
                # simple heuristic that the proper delimiter will produce more items when used for split
                if len(line.split(delim)) > maxItems:
                    maxItems = len(line.split(delim))
                    guessedDelim = delim

            return guessedDelim
        
        fid = open(self.filename, 'r')
        delims = [',','\t']
        delim = None # default

        while n < 10: # only look at first 10 data lines max
            line = fid.readline()
            if line.startswith('#'): #check for comments
                commentLines.append(line[1:])
            elif is_header_candidate(line, delims): # textual header that is not a comment
                headerNameLines.append(line)
                delim = guess_delim(line,delims)
            else:
                # upon first encounter we may need to check if ','-delimited or '\t'-delimited!
                if delim is None:
                    delim = guess_delim(line,delims)
                dataLines.append(line.split(delim))
                n += 1
                
        numCommentLines = len(commentLines)
        numCols = len(dataLines[0])
        numHeaderNameLines = len(headerNameLines)
        
        if len(headerNameLines) > 0 and len(headerNameLines[-1].split(delim)) == numCols:
            colNamesRaw = [s.strip() for s in headerNameLines[-1].split(delim)]
            # the stuff below seemed necessary since (1) some names came with byte order mark, or BOM, prepended
            # and (2) some had quotes around the names
            colNames = [name.encode('utf-8').decode('utf-8-sig').strip('"') for name in colNamesRaw]
        else:
            colNames = ['column_%d' % i for i in range(numCols)]

        self.colNames = colNames
        self.dataLines = dataLines
        self.nHeaderLines = numCommentLines + numHeaderNameLines
        self.guessedDelim = delim


    def replace_names(self):
        newnames = []
        if self.flavour == 'default':
            repdict = { # a few default translations, just in case
                'X' : 'x',
                'Y' : 'y',
                'Z' : 'z',
            }
        else:
            repdict = self.csv_flavours[self.flavour]['column_name_mappings']

        for name in self.colNames:
            if name in repdict.keys():
                newname = repdict[name]
            else:
                newname = name
            newnames.append(newname.replace(' ','_')) # in names we replace spaces with underscores
        self.translatedNames = newnames


    def check_required_names(self):
        reqNotDef = [name for name in self.requiredNames.keys() if not name in self.translatedNames]
        if len(reqNotDef) > 0:
            raise RuntimeError("some required names are not defined in file header: " + repr(reqNotDef))
            # this is a stopgap; the proper implementation will need to call into the textimportdialog
            # at this stage

            
    def read_csv_data(self):
        return np.genfromtxt(self.filename,
                             comments=self.flavour_value_or_default('comment_char','#'),
                             delimiter=self.flavour_value_or_default('delimiter',','),
                             skip_header=self.nHeaderLines,
                             skip_footer=self.flavour_value_or_default('skip_footer',0),
                             names=self.translatedNames, dtype='f4', replace_space='_',
                             missing_values=None, filling_values=np.nan, # use NaN to flag missing values
                             invalid_raise=not self.flavour_value_or_default('must_ignore_errors', False),
                             encoding='latin-1') # Zeiss Elyra bombs unless we go for latin-1 encoding, maybe make flavour specific?

    
    def check_flavour(self):
        for flavour in self.csv_flavours:
            if all(idn in self.colNames for idn in self.csv_flavours[flavour]['idnames']):
                self.flavour = flavour
        if self.guessedDelim is not None:
            # consistency check
            if self.flavour_value_or_default('delimiter',',') != self.guessedDelim:
                raise RuntimeError('guessed delimiter %s and flavour delimiter %s do not match' %
                                   (self.guessedDelim, self.flavour_value_or_default('delimiter',',')))


    def print_flavour(self):
        print('Flavour is %s' % self.flavour)

    def gen_mdh(self): # generate some metaData that will be passed up the chain
                       # to record some bits of this import
        mdh = MetaDataHandler.NestedClassMDHandler()
        mdh['SMLMImporter.flavour'] = self.flavour
        mdh['SMLMImporter.originalNames'] = self.colNames
        mdh['SMLMImporter.translatedNames'] = self.translatedNames
        
        self._mdh = mdh
       

    def get_mdh(self):
        if not hasattr(self,'_mdh'):
            self.gen_mdh()
        return self._mdh

    
    def read_csv_flavour(self):
        self.parse_header_csv()
        self.check_flavour()
        self.replace_names()
        self.check_required_names()
        
        data = self.read_csv_data()
        # remove rows with NaNs
        col_first = self.translatedNames[0]
        col_last = self.translatedNames[-1]
        if np.any(np.logical_or(np.isnan(data[col_first]),
                                np.isnan(data[col_last]))): # this only looks in first and last columns
                                                            # need a better and complete check
            data = data[np.logical_not(np.logical_or(np.isnan(data[col_first]),
                                                     np.isnan(data[col_last])))] # delete rows with missing values
        return data
