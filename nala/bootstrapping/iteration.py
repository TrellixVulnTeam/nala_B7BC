import glob
import json
import os
import re
import shutil
import time
import csv

from collections import defaultdict
from itertools import product, chain
from nala.bootstrapping.utils import UniprotDocumentSelector
from nala.bootstrapping.utils import PMIDDocumentSelector
from nala.bootstrapping.document_filters import QuickNalaFilter
from nala.bootstrapping.document_filters import KeywordsDocumentFilter, HighRecallRegexDocumentFilter, ManualDocumentFilter
from nala.bootstrapping.pmid_filters import AlreadyConsideredPMIDFilter
from nala.learning.postprocessing import PostProcessing
from nalaf import print_verbose
from nalaf.learning.crfsuite import CRFSuite, PyCRFSuite
from nalaf.structures.dataset_pipelines import PrepareDatasetPipeline
from nalaf.utils.annotation_readers import AnnJsonAnnotationReader, AnnJsonMergerAnnotationReader
from nalaf.utils.readers import HTMLReader
from nalaf.preprocessing.labelers import BIEOLabeler
from nalaf.learning.evaluators import MentionLevelEvaluator
from nalaf.utils.writers import TagTogFormat
from nala.preprocessing.definers import ExclusiveNLDefiner
from nalaf.learning.taggers import CRFSuiteTagger
from nala.utils import MUT_CLASS_ID, THRESHOLD_VALUE
from nalaf.structures.data import Entity
from nalaf.learning.taggers import GNormPlusGeneTagger
import csv

from nala.utils import get_prepare_pipeline_for_best_model


class Iteration:
    """
    This is the class to perform one iteration of bootstrapping. There are various options.
    """
    # todo finish docset of Iteration Class
    def __init__(self, folder=None, iteration_nr=None, crfsuite_path=None, threshold_val=THRESHOLD_VALUE):
        """
        Init function of iteration. Has to be called with proper folder and crfsuite path if not default.
        :param folder: Bootstrapping folder (has to be created before including base folder with html + annjson folder and corpus)
        :param iteration_nr: In which iteration the bootstrapping process is currently. Effects self.current_folder
        :param crfsuite_path: Folder of CRFSuite installation as full path or relative path to working dir. (gets converted to abspath)
        :param threshold_val: The threshold value to select annotations to pre-added or selected to semi-supervise.
        """
        super().__init__()
        # todo major sophisticated automatic execution (check what is missing e.g. bin_model)
        if folder is not None:
            self.bootstrapping_folder = os.path.abspath(folder)
        else:
            self.bootstrapping_folder = os.path.abspath("resources/bootstrapping")

        if not os.path.isdir(self.bootstrapping_folder):
            raise FileNotFoundError('''
            The bootstrapping folder does not exist.
            And needs to be created including with the annotated starting corpus.
            ''', self.bootstrapping_folder)

        if crfsuite_path is None:
            self.crfsuite_path = os.path.abspath(r'crfsuite')
        else:
            self.crfsuite_path = os.path.abspath(crfsuite_path)

        if not os.path.isdir(self.crfsuite_path):
            raise FileNotFoundError('''
            The CRFsuite folder does not exist.
            ''', self.crfsuite_path)

        # represents the iteration
        self.number = -1

        # threshold class-wide variable to save in stats.csv file
        self.threshold_val = threshold_val

        # empty init variables
        self.train = None  # first
        self.candidates = None  # non predicted docselected
        self.predicted = None  # predicted docselected
        # self.crf = CRFSuite(self.crfsuite_path, minify=True)
        self.crf = PyCRFSuite()

        # preparedataset pipeline init
        self.pipeline = get_prepare_pipeline_for_best_model()
        """ :type PrepareDatasetPipeline: """

        # labeler init
        self.labeler = BIEOLabeler()

        # discussion on config file in bootstrapping root or iteration_n check for n
        # note currently using parameter .. i think that s the most suitable

        print_verbose('Check for Iteration Number....')

        # TODO make state checks (e.g. has bin model, reviewed files, candidates, results)

        if iteration_nr is None:
            # find iteration number
            _iteration_name = self.bootstrapping_folder + "/iteration_*/"
            for fn in glob.glob(_iteration_name):
                match = re.search('iteration_([0-9]+)', fn)
                found_iteration = int(match.group(1))
                if found_iteration > self.number:
                    self.number = found_iteration

            # check for candidates and reviewed
            if os.path.isdir(os.path.join(self.bootstrapping_folder, "iteration_{}".format(self.number), 'candidates')):
                if os.path.isdir(os.path.join(self.bootstrapping_folder, "iteration_{}".format(self.number), 'reviewed')):
                    self.number += 1
            if self.number == 0:
                self.number += 1
        else:
            self.number = iteration_nr
        # current folders
        self.current_folder = os.path.join(self.bootstrapping_folder, "iteration_{}".format(self.number))
        self.candidates_folder = os.path.join(self.current_folder, 'candidates')
        self.reviewed_folder = os.path.join(self.current_folder, 'reviewed')

        if not os.path.exists(os.path.join(self.current_folder)):
            os.mkdir(os.path.join(self.current_folder))

        # binary model
        self.bin_model = os.path.join(self.current_folder, 'bin_model')

        # stats file
        self.stats_file = os.path.join(self.bootstrapping_folder, 'stats.csv')
        self.results_file = os.path.join(self.current_folder, 'batch_results.txt')
        self.debug_file = os.path.join(self.current_folder, 'debug.txt')

        print_verbose('Initialisation of Iteration instance finished.')

        if not os.path.exists(self.stats_file):
            with open(self.stats_file, 'w', newline='') as file:
                writer = csv.writer(file)
                writer.writerow(['iteration_number', 'subclass', 'threshold',
                                 'tp', 'fp', 'fn', 'fp_overlap', 'fn_overlap', 'precision', 'recall', 'f1-score'])

    def before_annotation(self, nr_new_docs=10):
        # self.read_learning_data()
        # self.preprocessing()
        # self.crf_learning()

        self.learning()
        self.docselection(nr=nr_new_docs)
        self.tagging(threshold_val=self.threshold_val)

    def after_annotation(self):
        self.clean_reviewed_files()
        self.manual_review_import()
        self.evaluation()

    def read_learning_data(self):
        """
        Loads and parses the annotations from base + following iterations into self.train
        """
        print_verbose("\n\n####ParseData####\n")

        base_folder = os.path.join(self.bootstrapping_folder, "iteration_0/base/")
        html_base_folder = base_folder + "html/"
        annjson_base_folder = base_folder + "annjson/"
        self.train = HTMLReader(html_base_folder).read()
        # TODO mergannotationreader --> change how to add annotations and read them from there...
        AnnJsonMergerAnnotationReader(os.path.join(annjson_base_folder, 'members'), strategy='intersection',
                                      entity_strategy='priority').annotate(self.train)

        # extend for each next iteration
        if self.number > 1:
            for i in range(1, self.number):
                # get new dataset
                path_to_read = os.path.join(self.bootstrapping_folder, "iteration_{}".format(i))
                tmp_data = HTMLReader(path_to_read + "/candidates/html/").read()
                AnnJsonAnnotationReader(path_to_read + "/reviewed/").annotate(tmp_data)

                # extend learning_data
                self.train.extend_dataset(tmp_data)

        print_verbose(len(self.train.documents), "documents are used in the training dataset.")

    def read_iteration_data(self, it_nr):
        """
        Method to return a dataset for a specificed iteration nr.
        Is useful to calculate the statistics for each iteration (amount of nl mentions and such).
        :param it_nr: int
        :rtype: nalaf.structures.data.Dataset()
        """
        print_verbose('####ReadIterationData####')
        path = os.path.join(self.bootstrapping_folder, 'iteration_' + str(it_nr))
        html = os.path.join(path, 'candidates', 'html')
        annjson = os.path.join(path, 'reviewed')

        data = HTMLReader(html).read()
        AnnJsonAnnotationReader(annjson).annotate(data)

        return data

    def check_iterations_stats(self):
        """
        At the moment just prints the stats for each iteration.
        Stats:
        * total mentions
        * st mentions
        * nl mentions
        * subclass 1 mentions and subclass 2 mentions
        * nl mentions per document ratio
        """
        row_format = "{:>10} | {:>5.2f}"
        mentions = []   # todo calc average stats
        for i in range(1, self.number):
            tmp_data = self.read_iteration_data(i)
            current_mentions = [0,0,0]
            ExclusiveNLDefiner().define(tmp_data)
            for ann in tmp_data.annotations():
                if ann.subclass == 0:
                    current_mentions[0] += 1
                elif ann.subclass == 1:
                    current_mentions[1] += 1
                elif ann.subclass == 2:
                    current_mentions[2] += 1

            print(row_format.format('iter', i))
            print(row_format.format('total', sum(current_mentions)))
            print(row_format.format('st', current_mentions[0]))
            print(row_format.format('nl + ss', current_mentions[1] + current_mentions[2]))
            print(row_format.format('nl', current_mentions[1]))
            print(row_format.format('ss', current_mentions[2]))
            print(row_format.format('nl+ss/doc', (current_mentions[1] + current_mentions[2])/10))
            mentions += current_mentions

    def preprocessing(self):
        """
        Pre-processing including pruning, generating features, generating labels.
        """
        print_verbose("\n\n####PreProcess####\n")
        # prune parts without annotations
        self.train.prune()

        # prepare features
        print_verbose("##PreparePipeline##")
        self.pipeline.execute(self.train)
        self.pipeline.serialize(self.train, to_file=self.debug_file)

        # labeling
        print_verbose("##Labeling##")
        self.labeler.label(self.train)

        print_verbose(len(self.train.documents), "documents are prepared for training dataset.")

    def crf_learning(self):
        """
        Learning: base + iterations 1..n-1
        just the crfsuitepart and copying the model to the iteration folder
        """
        print_verbose("\n\n####Learning####\n")

        # crfsuite part
        # self.crf.create_input_file(self.train, 'train')
        # self.crf.learn()
        self.crf.train(self.train, self.bin_model)

        # copy bin model to folder
        # shutil.copyfile(os.path.join(self.crfsuite_path, 'default_model'),
        #                 os.path.join(self.current_folder, 'bin_model'))


    def learning(self):
        """
        import files
        preprocess data
        run crfsuite on data
        """
        print_verbose("####learning####")
        self.read_learning_data()

        if not os.path.exists(self.bin_model):
            self.preprocessing()
            self.crf_learning()
        else:
            print_verbose("Already existing binary model is used.")

    def docselection(self, nr=2, just_caching=False):
        """
        Does the same as generate_documents(n) but the bootstrapping folder is specified in here.
        :param nr: amount of new documents wanted
        """
        print_verbose("\n\n\n======DocSelection======\n\n\n")
        from nalaf.structures.data import Dataset
        from nala.structures.selection_pipelines import DocumentSelectorPipeline
        from itertools import count
        c = count(1)

        dataset = Dataset()

        if just_caching:
            _counter = 0
            _total = 0
            with DocumentSelectorPipeline(
                    document_selector=UniprotDocumentSelector(),
                    pmid_filters=[AlreadyConsideredPMIDFilter(self.bootstrapping_folder, self.number)]
                    ) as dsp:
                _starttime = time.perf_counter()
                _already_downloaded = 0
                for pmid, document in dsp.execute():

                    dataset.documents[pmid] = document

                    _counter += 1
                    _tmptime = time.perf_counter()
                    _one_it = _tmptime - _starttime
                    _starttime = _tmptime

                    if _one_it < 0.25:  # check if not already downloaded (for eta calculation)
                        _already_downloaded += 1
                        _counter -= 1
                        _total -= _one_it

                    _total += _one_it
                    # print_verbose('total', _total)
                    if _counter > 0:
                        _eta_one = _total / _counter
                        _counter_left = nr - _counter - _already_downloaded
                        _eta_left = _eta_one * _counter_left
                        print_verbose(
                            'NrOfDocs: {} | ETA Left for {}: {:.3f} | ETA One for One: {:.3f}'.format(_counter, _counter_left,
                                                                                          _eta_left, _eta_one))

                    # if we have generated enough documents stop
                    if next(c) == nr:
                        break
        else:
            with DocumentSelectorPipeline(
                    document_selector=UniprotDocumentSelector(),
                    pmid_filters=[AlreadyConsideredPMIDFilter(self.bootstrapping_folder, self.number)],
                    document_filters=[HighRecallRegexDocumentFilter(crfsuite_path=self.crfsuite_path,
                                                                    binary_model=self.bin_model,
                                                                    expected_max_results=nr, use_nala=True),
                                      # QuickNalaFilter(binary_model=self.bin_model,
                                      #                 crfsuite_path=self.crfsuite_path, threshold=1),
                                      ManualDocumentFilter()]) as dsp:
                for pmid, document in dsp.execute():
                    dataset.documents[pmid] = document
                    # if we have generated enough documents stop
                    if next(c) == nr:
                        break

        self.candidates = dataset

    def tagging(self, threshold_val=THRESHOLD_VALUE):
        # tagging
        print_verbose("\n\n\n======Tagging======\n\n\n")
        # prepare dataset
        self.pipeline.execute(self.candidates)
        # crfsuite tagger
        # CRFSuiteTagger([MUT_CLASS_ID], self.crf).tag(self.candidates)
        self.crf.tag(self.candidates, self.bin_model)

        # postprocess
        PostProcessing().process(self.candidates)

        # gnorm tagger
        GNormPlusGeneTagger().tag(self.candidates)

        # export to anndoc format
        ttf_candidates = TagTogFormat(self.candidates, self.candidates_folder)
        ttf_candidates.export_html()
        ttf_candidates.export_ann_json(threshold_val)

    def manual_review_import(self):
        """
        Parse from iteration_n/reviewed folder in anndoc format.
        :return:
        """
        self.reviewed = HTMLReader(os.path.join(self.candidates_folder, 'html')).read()
        AnnJsonAnnotationReader(os.path.join(self.candidates_folder, 'annjson'), is_predicted=True,
                                delete_incomplete_docs=False).annotate(
            self.reviewed)
        AnnJsonAnnotationReader(os.path.join(self.reviewed_folder)).annotate(self.reviewed)
        # automatic evaluation

    def evaluation(self):
        """
        When Candidates and Reviewed are existing do automatic evaluation and calculate performances
        :return:
        """
        ExclusiveNLDefiner().define(self.reviewed)

        # debug results / annotations
        results = []
        for part in self.reviewed.parts():
            not_found_ann = part.annotations[:]
            not_found_pred = part.predicted_annotations[:]
            for ann, pred in product(part.annotations, part.predicted_annotations):
                Entity.equality_operator = 'exact_or_overlapping'
                if ann == pred:
                    results.append((ann, pred))

                    # delete found elements
                    if ann in not_found_ann:
                        index = not_found_ann.index(ann)
                        del not_found_ann[index]

                    if pred in not_found_pred:
                        index = not_found_pred.index(pred)
                        del not_found_pred[index]
            results += [(ann, Entity(class_id='e_2', offset=-1, text='')) for ann in not_found_ann]
            results += [(Entity(class_id='e_2', offset=-1, text=''), pred) for pred in not_found_pred]

        annotated_format = "{:<" + str(max(chain(len(x.text) for x in self.reviewed.annotations()))) + "}"
        predicted_format = "{:<" + str(max(chain(len(x.text) for x in self.reviewed.predicted_annotations()))) + "}"
        row_format = annotated_format + '\t|\t' + predicted_format + "\n"

        with open(self.results_file, 'w', encoding='utf-8') as f:
            f.write(row_format.format('=====Annotated=====', '=====Predicted====='))
            for tuple in ((x[0].text, x[1].text) for x in results):
                f.write(row_format.format(*tuple))
            f.write('-'*80)
            f.write('\n\n=====Detailed Results=====\n')
            f.write(
                'Exact:            TP={}\tFP={}\tFN={}\tFP_OVERLAP={}\tFN_OVERLAP={}\tPREC={:.3%}\tRECALL={:.3%}\tF-MEAS={:.3%}\n'.format(
                    *MentionLevelEvaluator().evaluate(self.reviewed)))
            f.write(
                'Overlapping:      TP={}\tFP={}\tFN={}\tFP_OVERLAP={}\tFN_OVERLAP={}\tPREC={:.3%}\tRECALL={:.3%}\tF-MEAS={:.3%}\n'.format(
                    *MentionLevelEvaluator(strictness='overlapping').evaluate(self.reviewed)))
            f.write(
                'Half-Overlapping: TP={}\tFP={}\tFN={}\tFP_OVERLAP={}\tFN_OVERLAP={}\tPREC={:.3%}\tRECALL={:.3%}\tF-MEAS={:.3%}\n'.format(
                    *MentionLevelEvaluator(strictness='half_overlapping').evaluate(self.reviewed)))
            subclass_string = json.dumps(MentionLevelEvaluator(subclass_analysis=True).evaluate(self.reviewed)[0],
                                         indent=4, sort_keys=True)
            f.write('Raw-Data:\n{}'.format(subclass_string))

        # optional containing sentence
        # optional containing document-id
        # optional group according to subclass (different sizes)

    def cross_validation(self, split):
        """
        does k fold cross validation with split being k
        :param split: int
        """
        py_crf = PyCRFSuite()

        base_folder = os.path.join(os.path.join(self.bootstrapping_folder, 'iteration_0'), 'base')
        data = HTMLReader(os.path.join(base_folder, 'html')).read()
        AnnJsonMergerAnnotationReader(os.path.join(os.path.join(base_folder, 'annjson'), 'members'),
                                      strategy='intersection', entity_strategy='priority').annotate(data)
        print_verbose(len(data), 'documents in base')

        for fold in range(1, self.number):
            iteration_base = os.path.join(self.bootstrapping_folder, "iteration_{}".format(fold))

            tmp_data = HTMLReader(os.path.join(os.path.join(iteration_base, 'candidates'), 'html')).read()
            AnnJsonAnnotationReader(os.path.join(iteration_base, 'reviewed')).annotate(tmp_data)
            data.extend_dataset(tmp_data)
        print_verbose(len(data), 'documents in total')

        last_iteration = os.path.join(self.bootstrapping_folder, "iteration_{}".format(self.number-1))
        cv_file = os.path.join(last_iteration, 'cross_validation.csv')
        with open(cv_file, 'w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(['fold', 'strictness', 'sublcass',
                             'tp', 'fp', 'fn', 'fp_overlap', 'fn_overlap',
                             'precision', 'recall', 'f1-score'])

        train_splits, test_splits = data.n_fold_split(split)

        folds_results_exact = []
        folds_results_overlapping = []
        subclass_averages_exact = defaultdict(list)
        subclass_averages_overlapping = defaultdict(list)

        pipeline = get_prepare_pipeline_for_best_model()
        for fold in range(split):
            print_verbose('starting with fold:', fold)
            train = train_splits[fold]
            test = test_splits[fold]

            train.prune()
            pipeline.execute(train)
            BIEOLabeler().label(train)
            py_crf.train(train, 'cross_validation_model')

            pipeline.execute(test)
            BIEOLabeler().label(test)
            py_crf.tag(test, 'cross_validation_model')

            ExclusiveNLDefiner().define(test)
            PostProcessing().process(test)

            with open(cv_file, 'a', newline='') as file:
                writer = csv.writer(file)

                subclass_measures, results = MentionLevelEvaluator(strictness='exact', subclass_analysis=True).evaluate(test)
                for subclass, measures in subclass_measures.items():
                    writer.writerow(list(chain([fold, 'exact', int(subclass)], measures)))
                    subclass_averages_exact[subclass].append(measures)
                writer.writerow(list(chain([fold, 'exact', 'total'], results)))
                folds_results_exact.append(results)

                subclass_measures, results = MentionLevelEvaluator(strictness='overlapping', subclass_analysis=True).evaluate(test)
                for subclass, measures in subclass_measures.items():
                    writer.writerow(list(chain([fold, 'overlapping', int(subclass)], measures)))
                    subclass_averages_overlapping[subclass].append(measures)
                writer.writerow(list(chain([fold, 'overlapping', 'total'], results)))
                folds_results_overlapping.append(results)

        # calculate and write average of folds
        with open(cv_file, 'a', newline='') as file:
            writer = csv.writer(file)
            # ================== EXACT =================
            for subclass, averages in subclass_averages_exact.items():
                writer.writerow(list(chain(['average', 'exact', subclass],
                                           [sum(col)/len(col) for col in zip(*averages)])))
            # average out everything in the columns
            writer.writerow(list(chain(['average', 'exact', 'total'],
                                       [sum(col)/len(col) for col in zip(*folds_results_exact)])))

            # =============== OVERLAPPING ===============
            for subclass, averages in subclass_averages_overlapping.items():
                writer.writerow(list(chain(['average', 'overlapping', subclass],
                                           [sum(col)/len(col) for col in zip(*averages)])))
            # average out everything in the columns
            writer.writerow(list(chain(['average', 'overlapping', 'total'],
                                       [sum(col)/len(col) for col in zip(*folds_results_overlapping)])))

            # =============== sum of folds ===============
            # sum up the counts (tp, fp, etc.) and then calculate the measures
            for subclass, averages in subclass_averages_exact.items():
                writer.writerow(list(chain(['sum_of_folds', 'exact', subclass],
                                           MentionLevelEvaluator(strictness='exact').calc_measures(
                                                   *[sum(col) for col in zip(*averages)][:5]))))
            writer.writerow(list(chain(['sum_of_folds', 'exact', 'total'],
                                       MentionLevelEvaluator(strictness='exact').calc_measures(
                                               *[sum(col) for col in zip(*folds_results_exact)][:5]))))

            with open(self.stats_file, 'a',  newline='') as stats_write_file:
                stats_writer = csv.writer(stats_write_file)
                for subclass, averages in subclass_averages_exact.items():
                    stats = MentionLevelEvaluator(strictness='overlapping').calc_measures(
                            *[sum(col) for col in zip(*averages)][:5])
                    writer.writerow(list(chain(['sum_of_folds', 'overlapping', subclass], stats)))
                    stats_writer.writerow([self.number-1, subclass, self.threshold_val] + list(stats))

                stats = MentionLevelEvaluator(strictness='overlapping').calc_measures(
                        *[sum(col) for col in zip(*folds_results_exact)][:5])
                writer.writerow(list(chain(['sum_of_folds', 'overlapping', 'total'], stats)))
                stats_writer.writerow([self.number-1, 'total', self.threshold_val] + list(stats))

    def clean_reviewed_files(self):
        import re
        candidates = HTMLReader(os.path.join(self.candidates_folder, 'html')).read()
        for (dirpath, dirnames, filenames) in os.walk(self.reviewed_folder):
            for filename in filenames:
                docid = re.sub(r'.*-(\d+)\..*', r'\1', filename)
                if docid not in candidates.documents.keys():
                    os.remove(os.path.join(dirpath, filename))
