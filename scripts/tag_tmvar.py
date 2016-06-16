import sys
import os
from nala.utils.corpora import get_corpus
from nala.learning.taggers import TmVarTagger
from nalaf.learning.evaluators import MentionLevelEvaluator
from nala.preprocessing.definers import ExclusiveNLDefiner
from nalaf.utils.writers import TagTogFormat

corpus_name = sys.argv[1]
preds_folder = sys.argv[2]
folder_name = os.path.join(preds_folder, 'tmVar', corpus_name)
is_predict = sys.argv[3] == "predict"

data = get_corpus(corpus_name)

def predict():
    with TmVarTagger() as t:
        t.tag(data)

    TagTogFormat(data, use_predicted=True, to_save_to=folder_name).export_ann_json(1)


def evaluate():
    from nalaf.utils.annotation_readers import AnnJsonAnnotationReader
    AnnJsonAnnotationReader(os.path.join(folder_name, "annjson"), is_predicted=True, delete_incomplete_docs=False).annotate(data)

    ExclusiveNLDefiner().define(data)
    e = MentionLevelEvaluator(subclass_analysis=True).evaluate(data)
    print(e)

if is_predict:
    predict()
else:
    evaluate()
