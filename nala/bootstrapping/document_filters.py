import abc
from copy import deepcopy
import json
import csv
import re
import time
import pkg_resources
from nalaf.learning.crfsuite import PyCRFSuite
from nalaf.preprocessing.labelers import BIEOLabeler
from nala.learning.postprocessing import PostProcessing
from nalaf import print_verbose, print_debug
from nala.preprocessing.definers import InclusiveNLDefiner
from nala.preprocessing.definers import ExclusiveNLDefiner
from nalaf.preprocessing.spliters import NLTKSplitter
from nalaf.structures.data import Dataset
from nalaf.utils.cache import Cacheable
from collections import Counter

from nala.utils import MUT_CLASS_ID
from nala.utils import get_prepare_pipeline_for_best_model

from nala.utils.pattern_eval import highlighted_text


class DocumentFilter:
    """
    Abstract base class for filtering out a list of documents (given as a Dataset object)
    according to some criterion.

    Subclasses that inherit this class should:
    * Be named [Name]DocumentFilter
    * Implement the abstract method filter as a generator
    meaning that if some criterion is fulfilled then [yield] that document

    When implementing filter first iterate for each PMID then apply logic to allow chaining of filters.
    """

    @abc.abstractmethod
    def filter(self, documents):
        """
        :type documents: collections.Iterable[nalaf.structures.data.Document]
        """
        pass


class StubDocumentFilter(DocumentFilter):
    """
    Class to provide quick passing through (can be helpful for pre-caching).
    """
    def filter(self, documents):
        for pmid, doc in documents:
            yield pmid, doc


class KeywordsDocumentFilter(DocumentFilter):
    """
    TODO document that this doesn't mean PubMed XML filters
    Filters our documents that do not contain any of the given keywords in any of their parts.
    """
    def __init__(self, keywords=None):
        if not keywords:
            keywords = ('mutat\w*', 'variat\w*', 'substit\w*', 'insert\w*', 'delet\w*', 'snp')
        self.keywords = keywords
        """the keywords which the document should contain"""

        self.keywords = (re.compile(keyword, re.IGNORECASE) for keyword in keywords)

    def filter(self, documents):
        """
        :type documents: collections.Iterable[(str, nalaf.structures.data.Document)]
        """
        for pmid, doc in documents:
            # if any part of the document contains any of the keywords
            # yield that document
            if any(any(keyword.search(part.text) for keyword in self.keywords)
                   for part in doc.parts.values()):
                yield pmid, doc


class ManualDocumentFilter(DocumentFilter, Cacheable):
    """
    Displays each document to the user on the standard console.
    The user inputs Yes/No as standard input to accept or reject the document.
    """
    def __init__(self):
        super().__init__()
        self.is_timed = False

    def filter(self, documents):
        """
        :type documents: collections.Iterable[(str, nalaf.structures.data.Document)]
        """
        for pmid, doc in documents:
            # if we can't find it in the cache
            # ask the user and save it to the cache
            if pmid not in self.cache:
                print('http://www.ncbi.nlm.nih.gov/pubmed/{}'.format(pmid))
                print(highlighted_text(doc.get_text()))
                answer = input('do? ')
                self.cache[pmid] = answer.lower() in ['yes', 'y']
                if answer.lower() == 's':
                    break

            if self.cache[pmid]:
                yield pmid, doc


class ManualStatsDocumentFilter(DocumentFilter, Cacheable):
    """
    Displays each document to the user on the standard console.
    The user inputs any of the accepted answers for document acceptance or the document is rejected.
    The exact answer is stored for the corresponding docid.
    """
    def __init__(self, yes_answers):
        super().__init__()
        self.is_timed = False
        self.yes_answers = [a.lower() for a in yes_answers]
        assert('no' not in self.yes_answers)
        self.answers = {}
        self.counter = Counter({s: 0 for s in (self.yes_answers + ['no'])})

    def filter(self, documents):
        """
        :type documents: collections.Iterable[(str, nalaf.structures.data.Document)]
        """
        for docid, doc in documents:
            # if we can't find it in the cache
            # ask the user and save it to the cache
            if docid not in self.cache:
                print('http://www.ncbi.nlm.nih.gov/pubmed/{}'.format(docid))
                print(highlighted_text(doc.get_text()))

                while True:
                    answer = input("\n{}\n\nDo? (or stop): ".format(self.counter)).lower()

                    if answer in self.yes_answers or answer == 'no' or answer == 'stop':
                        break

                if answer == 'stop':
                    return

                self.answers[docid] = answer
                self.counter.update([answer])
                self.cache[docid] = answer in self.yes_answers

            if self.cache[docid]:
                yield docid, doc


class QuickNalaFilter(DocumentFilter):
    def __init__(self, binary_model="nala/data/default_model", threshold=1, labeler=BIEOLabeler()):
        self.binary_model = binary_model
        """ location where binary model for nala (crfsuite) is saved """
        self.threshold = threshold
        """threshold for nala to include documents that contain overlapping annotations with confidence lower than set threshold"""
        self.pipeline = get_prepare_pipeline_for_best_model()
        """best features and hyperparameters"""
        self.labeler = labeler
        """used labeler"""

    def filter(self, documents):
        pycrf = PyCRFSuite(self.binary_model)
        for pmid, doc in documents:
            dataset = Dataset()
            dataset.documents[pmid] = doc
            self.pipeline.execute(dataset)
            self.labeler.label(dataset)
            pycrf.tag(dataset, MUT_CLASS_ID)
            PostProcessing().process(dataset)
            ExclusiveNLDefiner().define(dataset)
            total_nl_mentions = []
            for part in doc:
                # print(part.annotations)
                print_verbose('predicted_annotations:', part.predicted_annotations)
                nl_mentions = [(ann.text, ann.subclass, ann.confidence) for ann in part.predicted_annotations if ann.subclass != 0 and ann.confidence <= self.threshold]
                total_nl_mentions += nl_mentions
            if any(total_nl_mentions):
                print('nl mentions', json.dumps(total_nl_mentions, indent=4))
                yield pmid, doc
            print_verbose('nothing found')


class HighRecallRegexClassifier():

    def __init__(self, ST=True, NL=True):
        assert(ST or NL)

        self.patterns = []

        if ST:
            regex_st_file = pkg_resources.resource_filename('nala.data', 'regex_st.json')
            with open(regex_st_file, 'r') as f:
                conventions = json.loads(f.read())
                self.patterns += [re.compile(x) for x in conventions]

            tmvarregex_file = pkg_resources.resource_filename('nala.data', 'RegEx.NL')
            with open(tmvarregex_file) as file:
                raw_regexps = list(csv.reader(file, delimiter='\t'))
                regexps = [x[0] for x in raw_regexps if len(x[0]) < 265]
                self.patterns = [re.compile(x) for x in regexps]

        if NL:
            pattern_file_name = pkg_resources.resource_filename('nala.data', 'nl_patterns.json')

            with open(pattern_file_name, 'r') as f:
                regexs = json.load(f)
                self.patterns += [re.compile(x) for x in regexs]


    def __call__(self, text):
        return any(p.search(text) for p in self.patterns)


class HighRecallRegexDocumentFilter(DocumentFilter):
    """
    Filter that uses regular expression to first get possible natural language mentions in sentences.
    Then each possible nl mention gets compared to tmVar results and Nala predictions. If there is no overlap,
    then this annotation will be considered as nl mention and thus the document will not be filtered out.
    Ever other document gets filtered out at this point.

    Condition for being filtered out:
    Not any(sentence that contains valid nl mention according to this definition)

    tmVar will be used in early stages and discarded as soon as there are no more results, thus gets a parameter.
    """

    def __init__(self, binary_model="nala/data/default_model", override_cache=False, expected_max_results=10,
                 pattern_file_name=None, threshold=1, min_found=1, use_nala=False, labeler=BIEOLabeler()):
        self.location_binary_model = binary_model
        """ location where binary model for nala (crfsuite) is saved """
        self.override_cache=override_cache
        """ tmvar results are saved in cache and reused from there.
        this option allows to force requesting results from tmVar online """
        self.expected_maximum_results=expected_max_results
        """ :returns maximum of [x] documents (can be less if not found) """
        self.threshold=threshold
        """threshold for nala to include documents that contain overlapping annotations with confidence lower than set threshold"""
        self.pipeline=get_prepare_pipeline_for_best_model()
        """ best setting (features, etc.) for tagging """
        self.min_found = min_found
        """ minimum found """
        self.use_nala = use_nala
        """ if use nala predictions """
        self.labeler = labeler
        """ the used labeler """

        # read in nl_patterns
        if not pattern_file_name:
            pattern_file_name = pkg_resources.resource_filename('nala.data', 'nl_patterns.json')
            # todo make {AA} substitutions in code to make regular expressions more readable

        with open(pattern_file_name, 'r') as f:
            regexs = json.load(f)
            self.patterns = [re.compile(x) for x in regexs]
            """ compiled regex patterns from pattern_file param to specify custom json file,
             containing regexs for high recall finding of nl mentions. (or sth else) """

    def filter(self, documents, min_found=1, use_nala=False):
        """
        :type documents: collections.Iterable[(str, nalaf.structures.data.Document)]
        """

        _progress = 1
        _start_time = time.time()
        _total_time = 0

        _time_avg_per_pattern = 0
        _pattern_calls = 0
        _time_reg_pattern_total = 0
        _time_max_pattern = 0
        _low_performant_pattern = ""

        # NLDefiners init
        exclusive_definer = ExclusiveNLDefiner()
        _e_array = [0, 0, 0]
        inclusive_definer = InclusiveNLDefiner()
        _i_array = [0, 0]

        last_found = 0
        crf = PyCRFSuite(self.location_binary_model)

        # counter_to_stop_for_caching = 0

        for pmid, doc in documents:
            # if any part of the document contains any of the keywords
            # yield that document

            # if counter_to_stop_for_caching > 400:
            #     break
            # counter_to_stop_for_caching += 1
            # print(counter_to_stop_for_caching)

            part_offset = 0
            data_tmp = Dataset()
            data_tmp.documents[pmid] = doc
            data_nala = deepcopy(data_tmp)
            NLTKSplitter().split(data_tmp)
            # data_tmvar = TmVarTagger().generate_abstracts([pmid])
            if use_nala:
                self.pipeline.execute(data_nala)
                self.labeler.label(data_nala)
                crf.tag(data_nala, MUT_CLASS_ID)
                PostProcessing().process(data_nala)
                ExclusiveNLDefiner().define(data_nala)

            used_regexs = {}

            positive_sentences = 0
            for i, x in enumerate(doc.parts):
                # print("Part", i)
                sent_offset = 0
                cur_part = doc.parts.get(x)
                sentences = cur_part.sentences_

                for sent in sentences:
                    sent_length = len(sent)
                    new_text = sent.lower()
                    new_text = re.sub('[\./\\-(){}\[\],%]', ' ', new_text)
                    # new_text = re.sub('\W+', ' ', new_text)

                    found_in_sentence = False

                    for i, reg in enumerate(self.patterns):
                        _lasttime = time.time()  # time start var
                        match = reg.search(new_text)

                        # debug bottleneck patterns
                        _time_current_reg = time.time() - _lasttime  # time end var
                        _pattern_calls += 1  # pattern calls already occured
                        _time_reg_pattern_total += _time_current_reg  # total time spent on searching with patterns
                        if _time_reg_pattern_total > 0:
                            _time_avg_per_pattern = _time_reg_pattern_total / _pattern_calls  # avg spent time per pattern call
                        # todo create pattern performance eval for descending amount of recognized patterns
                        # if _pattern_calls > len(patterns) * 20 and _time_avg_per_pattern * 10000 < _time_current_reg:
                        #     print("BAD_PATTERN_PERFORMANCE:", _time_avg_per_pattern, _time_current_reg, reg.pattern)
                        # if _time_max_pattern < _time_current_reg:
                        #     _time_max_pattern = _time_current_reg
                        #     _low_performant_pattern = reg.pattern
                        #     print(_time_avg_per_pattern, _low_performant_pattern, _time_max_pattern)

                        # if reg.pattern == r'(\b\w*\d+\w*\b\s?){1,3} (\b\w+\b\s?){1,4} (\b\w*\d+\w*\b\s?){1,3} (\b\w+\b\s?){1,4} (deletion|deleting|deleted)':
                        #     if _time_current_reg > _time_avg_per_pattern * 10:
                        #         # print(_time_avg_per_pattern, _time_current_reg)
                        #         f.write("BAD_PATTERN\n")
                        #         f.write(sent + "\n")
                        #         f.write(new_text + "\n")
                        if match:
                            # if pmid in data_tmvar.documents:
                            #     anti_doc = data_tmvar.documents.get(pmid)
                            nala_doc = data_nala.documents.get(pmid)

                            start = part_offset + sent_offset + match.span()[0]
                            end = part_offset + sent_offset + match.span()[1]
                            # print("TmVar is not overlapping?:", not anti_doc.overlaps_with_mention(start, end))
                            # print(not nala_doc.overlaps_with_mention(start, end, annotated=False))


                            if reg.pattern in used_regexs:
                                used_regexs[reg.pattern] += 1
                            else:
                                used_regexs[reg.pattern] = 1
                            print(color.PURPLE + new_text.replace(match.group(),
                                                                  color.BOLD + color.DARKCYAN + color.UNDERLINE + match.group() + color.END + color.PURPLE) + color.END)
                            if not found_in_sentence:
                                positive_sentences += 1
                                found_in_sentence = True
                                            # if not anti_doc.overlaps_with_mention(start,
                                            #                                       end) \
                                            #         and not nala_doc.overlaps_with_mention(start, end, annotated=False):
                                            #     _e_result = exclusive_definer.define_string(
                                            #         new_text[match.span()[0]:match.span()[1]])
                                            #     _e_array[_e_result] += 1
                                            #     _i_result = inclusive_definer.define_string(
                                            #         new_text[match.span()[0]:match.span()[1]])
                                            #     _i_array[_i_result] += 1
                                            # todo write to file param + saving to manually annotate and find tp + fp for performance eval on each pattern
                                            # print("e{}\ti{}\t{}\t{}\t{}\n".format(_e_result, _i_result, sent, match, reg.pattern))

                                            # last_found += 1
                                            # found_in_sentence = True
                                # else:
                                #     # if nala not used only tmvar considered
                                #     if not anti_doc.overlaps_with_mention(start, end):
                                #         _e_result = exclusive_definer.define_string(
                                #             new_text[match.span()[0]:match.span()[1]])
                                #         _e_array[_e_result] += 1
                                #         _i_result = inclusive_definer.define_string(
                                #             new_text[match.span()[0]:match.span()[1]])
                                #         _i_array[_i_result] += 1
                                #         # todo write to file param + saving to manually annotate and find tp + fp for performance eval on each pattern
                                #         # print("e{}\ti{}\t{}\t{}\t{}\n".format(_e_result, _i_result, sent, match, reg.pattern))
                                #         last_found += 1
                                #         found_in_sentence = True

                            if use_nala:
                                nala_found_mention = nala_doc.overlaps_with_mention(start, end, annotated=False)
                                if nala_found_mention:
                                    print_verbose(nala_found_mention)
                                    if nala_found_mention.subclass > 0 and nala_found_mention.confidence <= self.threshold:
                                        yield pmid, doc

                        if _lasttime - time.time() > 1:
                            print_verbose('time intensive regex', i)
                    sent_offset += 2 + sent_length

                    # for per sentence positives
                    if found_in_sentence:
                        positive_sentences += 1

                part_offset += sent_offset
            if use_nala:
                for part in nala_doc:
                    for ann in part.predicted_annotations:
                        if ann.subclass > 0:
                            print_verbose(part.text[:ann.offset] + color.BOLD + ann.text + color.END + part.text[
                                                                                                       ann.offset + len(
                                                                                                           ann.text):])
                            positive_sentences += min_found
            _old_time = _start_time
            _start_time = time.time()
            _one_time = _start_time - _old_time

            if _one_time > 0.3 and positive_sentences > min_found:
                _progress += 1
                _total_time += _one_time

            _time_per_doc = _total_time / _progress
            print_verbose("PROGRESS: {:.2f} secs ETA per one positive document:"
                          " {:.2f} secs".format(_total_time, _time_per_doc))
            print_debug('used regular expressions:', json.dumps(used_regexs, indent=4))
            if positive_sentences >= min_found:
                last_found = 0
                print_verbose('YEP', pmid)
                yield pmid, doc
            else:
                print_verbose('NOPE', pmid)


class color:
    PURPLE = '\033[95m'
    CYAN = '\033[96m'
    DARKCYAN = '\033[36m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BRIGHT = '\033[1m'
    DIM = '\033[2m'
    NBRIGHT = '\033[22m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'
