import logging
import os
import warnings
import typing
from collections import defaultdict, namedtuple
from typing import Any, Dict, List, Optional, Text, Tuple

import rasa.utils.io
from rasa.constants import RESULTS_FILE, PERCENTAGE_KEY
from rasa.core.utils import pad_lists_to_size
from rasa.core.events import ActionExecuted, UserUttered
from rasa.nlu.training_data.formats.markdown import MarkdownWriter
from rasa.core.trackers import DialogueStateTracker
from rasa.utils.io import DEFAULT_ENCODING

if typing.TYPE_CHECKING:
    from rasa.core.agent import Agent

import matplotlib

# At first, matplotlib will be initialized with default OS-specific available backend
# if that didn't happen, we'll try to set it up manually
if matplotlib.get_backend() is not None:
    pass
else:  # pragma: no cover
    try:
        # If the `tkinter` package is available, we can use the `TkAgg` backend
        import tkinter

        matplotlib.use("TkAgg")
    except ImportError:
        matplotlib.use("agg")


logger = logging.getLogger(__name__)

StoryEvalution = namedtuple(
    "StoryEvaluation",
    "evaluation_store failed_stories action_list in_training_data_fraction",
)


class EvaluationStore:
    """Class storing action, intent and entity predictions and targets."""

    def __init__(
        self,
        action_predictions: Optional[List[str]] = None,
        action_targets: Optional[List[str]] = None,
        intent_predictions: Optional[List[str]] = None,
        intent_targets: Optional[List[str]] = None,
        entity_predictions: Optional[List[Dict[Text, Any]]] = None,
        entity_targets: Optional[List[Dict[Text, Any]]] = None,
    ) -> None:
        self.action_predictions = action_predictions or []
        self.action_targets = action_targets or []
        self.intent_predictions = intent_predictions or []
        self.intent_targets = intent_targets or []
        self.entity_predictions = entity_predictions or []
        self.entity_targets = entity_targets or []

    def add_to_store(
        self,
        action_predictions: Optional[List[str]] = None,
        action_targets: Optional[List[str]] = None,
        intent_predictions: Optional[List[str]] = None,
        intent_targets: Optional[List[str]] = None,
        entity_predictions: Optional[List[Dict[Text, Any]]] = None,
        entity_targets: Optional[List[Dict[Text, Any]]] = None,
    ) -> None:
        """Add items or lists of items to the store"""
        for k, v in locals().items():
            if k != "self" and v:
                attr = getattr(self, k)
                if isinstance(v, list):
                    attr.extend(v)
                else:
                    attr.append(v)

    def merge_store(self, other: "EvaluationStore") -> None:
        """Add the contents of other to self"""
        self.add_to_store(
            action_predictions=other.action_predictions,
            action_targets=other.action_targets,
            intent_predictions=other.intent_predictions,
            intent_targets=other.intent_targets,
            entity_predictions=other.entity_predictions,
            entity_targets=other.entity_targets,
        )

    def has_prediction_target_mismatch(self):
        return (
            self.intent_predictions != self.intent_targets
            or self.entity_predictions != self.entity_targets
            or self.action_predictions != self.action_targets
        )

    def serialise(self) -> Tuple[List[Text], List[Text]]:
        """Turn targets and predictions to lists of equal size for sklearn."""

        targets = (
            self.action_targets
            + self.intent_targets
            + [
                MarkdownWriter.generate_entity_md(gold.get("text"), gold)
                for gold in self.entity_targets
            ]
        )
        predictions = (
            self.action_predictions
            + self.intent_predictions
            + [
                MarkdownWriter.generate_entity_md(predicted.get("text"), predicted)
                for predicted in self.entity_predictions
            ]
        )

        # sklearn does not cope with lists of unequal size, nor None values
        return pad_lists_to_size(targets, predictions, padding_value="None")


class WronglyPredictedAction(ActionExecuted):
    """The model predicted the wrong action.

    Mostly used to mark wrong predictions and be able to
    dump them as stories."""

    type_name = "wrong_action"

    def __init__(
        self, correct_action, predicted_action, policy, confidence, timestamp=None
    ):
        self.predicted_action = predicted_action
        super().__init__(correct_action, policy, confidence, timestamp=timestamp)

    def as_story_string(self):
        return "{}   <!-- predicted: {} -->".format(
            self.action_name, self.predicted_action
        )


class EndToEndUserUtterance(UserUttered):
    """End-to-end user utterance.

    Mostly used to print the full end-to-end user message in the
    `failed_stories.md` output file."""

    def as_story_string(self, e2e=True):
        return super().as_story_string(e2e=True)


class WronglyClassifiedUserUtterance(UserUttered):
    """The NLU model predicted the wrong user utterance.

    Mostly used to mark wrong predictions and be able to
    dump them as stories."""

    type_name = "wrong_utterance"

    def __init__(self, event: UserUttered, eval_store: EvaluationStore):

        if not eval_store.intent_predictions:
            self.predicted_intent = None
        else:
            self.predicted_intent = eval_store.intent_predictions[0]
        self.predicted_entities = eval_store.entity_predictions

        intent = {"name": eval_store.intent_targets[0]}

        super().__init__(
            event.text,
            intent,
            eval_store.entity_targets,
            event.parse_data,
            event.timestamp,
            event.input_channel,
        )

    def as_story_string(self, e2e=True):
        from rasa.core.events import md_format_message

        correct_message = md_format_message(self.text, self.intent, self.entities)
        predicted_message = md_format_message(
            self.text, self.predicted_intent, self.predicted_entities
        )
        return "{}: {}   <!-- predicted: {}: {} -->".format(
            self.intent.get("name"),
            correct_message,
            self.predicted_intent,
            predicted_message,
        )


async def _generate_trackers(resource_name, agent, max_stories=None, use_e2e=False):
    from rasa.core.training.generator import TrainingDataGenerator

    from rasa.core import training

    story_graph = await training.extract_story_graph(
        resource_name, agent.domain, agent.interpreter, use_e2e
    )
    g = TrainingDataGenerator(
        story_graph,
        agent.domain,
        use_story_concatenation=False,
        augmentation_factor=0,
        tracker_limit=max_stories,
    )
    return g.generate()


def _clean_entity_results(
    text: Text, entity_results: List[Dict[Text, Any]]
) -> List[Dict[Text, Any]]:
    """Extract only the token variables from an entity dict."""
    cleaned_entities = []

    for r in tuple(entity_results):
        cleaned_entity = {"text": text}
        for k in ("start", "end", "entity", "value"):
            if k in set(r):
                cleaned_entity[k] = r[k]
        cleaned_entities.append(cleaned_entity)

    return cleaned_entities


def _collect_user_uttered_predictions(
    event: UserUttered,
    partial_tracker: DialogueStateTracker,
    fail_on_prediction_errors: bool,
) -> EvaluationStore:
    user_uttered_eval_store = EvaluationStore()

    intent_gold = event.parse_data.get("true_intent")
    predicted_intent = event.parse_data.get("intent", {}).get("name")

    if not predicted_intent:
        predicted_intent = [None]

    user_uttered_eval_store.add_to_store(
        intent_predictions=predicted_intent, intent_targets=intent_gold
    )

    entity_gold = event.parse_data.get("true_entities")
    predicted_entities = event.parse_data.get("entities")

    if entity_gold or predicted_entities:
        user_uttered_eval_store.add_to_store(
            entity_targets=_clean_entity_results(event.text, entity_gold),
            entity_predictions=_clean_entity_results(event.text, predicted_entities),
        )

    if user_uttered_eval_store.has_prediction_target_mismatch():
        partial_tracker.update(
            WronglyClassifiedUserUtterance(event, user_uttered_eval_store)
        )
        if fail_on_prediction_errors:
            raise ValueError(
                "NLU model predicted a wrong intent. Failed Story:"
                " \n\n{}".format(partial_tracker.export_stories())
            )
    else:
        end_to_end_user_utterance = EndToEndUserUtterance(
            event.text, event.intent, event.entities
        )
        partial_tracker.update(end_to_end_user_utterance)

    return user_uttered_eval_store


def _emulate_form_rejection(processor, partial_tracker):
    from rasa.core.policies.form_policy import FormPolicy
    from rasa.core.events import ActionExecutionRejected

    if partial_tracker.active_form.get("name"):
        for p in processor.policy_ensemble.policies:
            if isinstance(p, FormPolicy):
                # emulate form rejection
                partial_tracker.update(
                    ActionExecutionRejected(partial_tracker.active_form["name"])
                )
                # check if unhappy path is covered by the train stories
                if not p.state_is_unhappy(partial_tracker, processor.domain):
                    # this state is not covered by the stories
                    del partial_tracker.events[-1]
                    partial_tracker.active_form["rejected"] = False


def _collect_action_executed_predictions(
    processor, partial_tracker, event, fail_on_prediction_errors
):
    from rasa.core.policies.form_policy import FormPolicy

    action_executed_eval_store = EvaluationStore()

    gold = event.action_name

    action, policy, confidence = processor.predict_next_action(partial_tracker)
    predicted = action.name()

    if policy and predicted != gold and FormPolicy.__name__ in policy:
        # FormPolicy predicted wrong action
        # but it might be Ok if form action is rejected
        _emulate_form_rejection(processor, partial_tracker)
        # try again
        action, policy, confidence = processor.predict_next_action(partial_tracker)
        predicted = action.name()

    action_executed_eval_store.add_to_store(
        action_predictions=predicted, action_targets=gold
    )

    if action_executed_eval_store.has_prediction_target_mismatch():
        partial_tracker.update(
            WronglyPredictedAction(
                gold, predicted, event.policy, event.confidence, event.timestamp
            )
        )
        if fail_on_prediction_errors:
            error_msg = (
                "Model predicted a wrong action. Failed Story: "
                "\n\n{}".format(partial_tracker.export_stories())
            )
            if FormPolicy.__name__ in policy:
                error_msg += (
                    "FormAction is not run during "
                    "evaluation therefore it is impossible to know "
                    "if validation failed or this story is wrong. "
                    "If the story is correct, add it to the "
                    "training stories and retrain."
                )
            raise ValueError(error_msg)
    else:
        partial_tracker.update(event)

    return action_executed_eval_store, policy, confidence


def _predict_tracker_actions(
    tracker, agent: "Agent", fail_on_prediction_errors=False, use_e2e=False
):
    from rasa.core.trackers import DialogueStateTracker

    processor = agent.create_processor()
    tracker_eval_store = EvaluationStore()

    events = list(tracker.events)

    partial_tracker = DialogueStateTracker.from_events(
        tracker.sender_id, events[:1], agent.domain.slots
    )

    tracker_actions = []

    for event in events[1:]:
        if isinstance(event, ActionExecuted):
            (
                action_executed_result,
                policy,
                confidence,
            ) = _collect_action_executed_predictions(
                processor, partial_tracker, event, fail_on_prediction_errors
            )
            tracker_eval_store.merge_store(action_executed_result)
            tracker_actions.append(
                {
                    "action": action_executed_result.action_targets[0],
                    "predicted": action_executed_result.action_predictions[0],
                    "policy": policy,
                    "confidence": confidence,
                }
            )
        elif use_e2e and isinstance(event, UserUttered):
            user_uttered_result = _collect_user_uttered_predictions(
                event, partial_tracker, fail_on_prediction_errors
            )

            tracker_eval_store.merge_store(user_uttered_result)
        else:
            partial_tracker.update(event)

    return tracker_eval_store, partial_tracker, tracker_actions


def _in_training_data_fraction(action_list):
    """Given a list of action items, returns the fraction of actions

    that were predicted using one of the Memoization policies."""
    from rasa.core.policies.ensemble import SimplePolicyEnsemble

    in_training_data = [
        a["action"]
        for a in action_list
        if a["policy"] and not SimplePolicyEnsemble.is_not_memo_policy(a["policy"])
    ]

    return len(in_training_data) / len(action_list)


def collect_story_predictions(
    completed_trackers: List["DialogueStateTracker"],
    agent: "Agent",
    fail_on_prediction_errors: bool = False,
    use_e2e: bool = False,
) -> Tuple[StoryEvalution, int]:
    """Test the stories from a file, running them through the stored model."""
    from rasa.nlu.test import get_evaluation_metrics
    from tqdm import tqdm

    story_eval_store = EvaluationStore()
    failed = []
    correct_dialogues = []
    number_of_stories = len(completed_trackers)

    logger.info(f"Evaluating {number_of_stories} stories\nProgress:")

    action_list = []

    for tracker in tqdm(completed_trackers):
        tracker_results, predicted_tracker, tracker_actions = _predict_tracker_actions(
            tracker, agent, fail_on_prediction_errors, use_e2e
        )

        story_eval_store.merge_store(tracker_results)

        action_list.extend(tracker_actions)

        if tracker_results.has_prediction_target_mismatch():
            # there is at least one wrong prediction
            failed.append(predicted_tracker)
            correct_dialogues.append(0)
        else:
            correct_dialogues.append(1)

    logger.info("Finished collecting predictions.")
    with warnings.catch_warnings():
        from sklearn.exceptions import UndefinedMetricWarning

        warnings.simplefilter("ignore", UndefinedMetricWarning)
        report, precision, f1, accuracy = get_evaluation_metrics(
            [1] * len(completed_trackers), correct_dialogues
        )

    in_training_data_fraction = _in_training_data_fraction(action_list)

    log_evaluation_table(
        [1] * len(completed_trackers),
        "END-TO-END" if use_e2e else "CONVERSATION",
        report,
        precision,
        f1,
        accuracy,
        in_training_data_fraction,
        include_report=False,
    )

    return (
        StoryEvalution(
            evaluation_store=story_eval_store,
            failed_stories=failed,
            action_list=action_list,
            in_training_data_fraction=in_training_data_fraction,
        ),
        number_of_stories,
    )


def log_failed_stories(failed, out_directory):
    """Take stories as a list of dicts."""
    if not out_directory:
        return
    with open(
        os.path.join(out_directory, "failed_stories.md"), "w", encoding=DEFAULT_ENCODING
    ) as f:
        if len(failed) == 0:
            f.write("<!-- All stories passed -->")
        else:
            for failure in failed:
                f.write(failure.export_stories())
                f.write("\n\n")


async def test(
    stories: Text,
    agent: "Agent",
    max_stories: Optional[int] = None,
    out_directory: Optional[Text] = None,
    fail_on_prediction_errors: bool = False,
    e2e: bool = False,
    disable_plotting: bool = False,
):
    """Run the evaluation of the stories, optionally plot the results."""
    from rasa.nlu.test import get_evaluation_metrics

    completed_trackers = await _generate_trackers(stories, agent, max_stories, e2e)

    story_evaluation, _ = collect_story_predictions(
        completed_trackers, agent, fail_on_prediction_errors, e2e
    )

    evaluation_store = story_evaluation.evaluation_store

    with warnings.catch_warnings():
        from sklearn.exceptions import UndefinedMetricWarning

        warnings.simplefilter("ignore", UndefinedMetricWarning)

        targets, predictions = evaluation_store.serialise()
        report, precision, f1, accuracy = get_evaluation_metrics(targets, predictions)

    if out_directory:
        plot_story_evaluation(
            evaluation_store.action_targets,
            evaluation_store.action_predictions,
            report,
            precision,
            f1,
            accuracy,
            story_evaluation.in_training_data_fraction,
            out_directory,
            disable_plotting,
        )

    log_failed_stories(story_evaluation.failed_stories, out_directory)

    return {
        "report": report,
        "precision": precision,
        "f1": f1,
        "accuracy": accuracy,
        "actions": story_evaluation.action_list,
        "in_training_data_fraction": story_evaluation.in_training_data_fraction,
        "is_end_to_end_evaluation": e2e,
    }


def log_evaluation_table(
    golds,
    name,
    report,
    precision,
    f1,
    accuracy,
    in_training_data_fraction,
    include_report=True,
):  # pragma: no cover
    """Log the sklearn evaluation metrics."""
    logger.info(f"Evaluation Results on {name} level:")
    logger.info(
        "\tCorrect:          {} / {}".format(int(len(golds) * accuracy), len(golds))
    )
    logger.info(f"\tF1-Score:         {f1:.3f}")
    logger.info(f"\tPrecision:        {precision:.3f}")
    logger.info(f"\tAccuracy:         {accuracy:.3f}")
    logger.info(f"\tIn-data fraction: {in_training_data_fraction:.3g}")

    if include_report:
        logger.info(f"\tClassification report: \n{report}")


def plot_story_evaluation(
    test_y,
    predictions,
    report,
    precision,
    f1,
    accuracy,
    in_training_data_fraction,
    out_directory,
    disable_plotting,
):
    """Plot the results of story evaluation"""
    from sklearn.metrics import confusion_matrix
    from sklearn.utils.multiclass import unique_labels
    import matplotlib.pyplot as plt
    from rasa.nlu.test import plot_confusion_matrix

    log_evaluation_table(
        test_y,
        "ACTION",
        report,
        precision,
        f1,
        accuracy,
        in_training_data_fraction,
        include_report=True,
    )

    if disable_plotting:
        return

    cnf_matrix = confusion_matrix(test_y, predictions)

    plot_confusion_matrix(
        cnf_matrix,
        classes=unique_labels(test_y, predictions),
        title="Action Confusion matrix",
    )

    fig = plt.gcf()
    fig.set_size_inches(int(20), int(20))
    fig.savefig(os.path.join(out_directory, "story_confmat.pdf"), bbox_inches="tight")


async def compare_models_in_dir(
    model_dir: Text, stories_file: Text, output: Text
) -> None:
    """Evaluates multiple trained models in a directory on a test set."""
    import rasa.utils.io as io_utils

    number_correct = defaultdict(list)

    for run in io_utils.list_subdirectories(model_dir):
        number_correct_in_run = defaultdict(list)

        for model in sorted(io_utils.list_files(run)):
            if not model.endswith("tar.gz"):
                continue

            # The model files are named like <config-name>PERCENTAGE_KEY<number>.tar.gz
            # Remove the percentage key and number from the name to get the config name
            config_name = os.path.basename(model).split(PERCENTAGE_KEY)[0]
            number_of_correct_stories = await _evaluate_core_model(model, stories_file)
            number_correct_in_run[config_name].append(number_of_correct_stories)

        for k, v in number_correct_in_run.items():
            number_correct[k].append(v)

    rasa.utils.io.dump_obj_as_json_to_file(
        os.path.join(output, RESULTS_FILE), number_correct
    )


async def compare_models(models: List[Text], stories_file: Text, output: Text) -> None:
    """Evaluates provided trained models on a test set."""

    number_correct = defaultdict(list)

    for model in models:
        number_of_correct_stories = await _evaluate_core_model(model, stories_file)
        number_correct[os.path.basename(model)].append(number_of_correct_stories)

    rasa.utils.io.dump_obj_as_json_to_file(
        os.path.join(output, RESULTS_FILE), number_correct
    )


async def _evaluate_core_model(model: Text, stories_file: Text) -> int:
    from rasa.core.agent import Agent

    logger.info(f"Evaluating model '{model}'")

    agent = Agent.load(model)
    completed_trackers = await _generate_trackers(stories_file, agent)
    story_eval_store, number_of_stories = collect_story_predictions(
        completed_trackers, agent
    )
    failed_stories = story_eval_store.failed_stories
    return number_of_stories - len(failed_stories)


def plot_nlu_results(output: Text, number_of_examples: List[int]) -> None:

    graph_path = os.path.join(output, "nlu_model_comparison_graph.pdf")

    _plot_curve(
        output,
        number_of_examples,
        x_label_text="Number of intent examples present during training",
        y_label_text="Label-weighted average F1 score on test set",
        graph_path=graph_path,
    )


def plot_core_results(output: Text, number_of_examples: List[int]) -> None:

    graph_path = os.path.join(output, "core_model_comparison_graph.pdf")

    _plot_curve(
        output,
        number_of_examples,
        x_label_text="Number of stories present during training",
        y_label_text="Number of correct test stories",
        graph_path=graph_path,
    )


def _plot_curve(
    output: Text,
    number_of_examples: List[int],
    x_label_text: Text,
    y_label_text: Text,
    graph_path: Text,
) -> None:
    """Plot the results from a model comparison.

    Args:
        output: Output directory to save resulting plots to
        number_of_examples: Number of examples per run
        x_label_text: text for the x axis
        y_label_text: text for the y axis
        graph_path: output path of the plot
    """
    import matplotlib.pyplot as plt
    import numpy as np
    import rasa.utils.io

    ax = plt.gca()

    # load results from file
    data = rasa.utils.io.read_json_file(os.path.join(output, RESULTS_FILE))
    x = number_of_examples

    # compute mean of all the runs for different configs
    for label in data.keys():
        if len(data[label]) == 0:
            continue
        mean = np.mean(data[label], axis=0)
        std = np.std(data[label], axis=0)
        ax.plot(x, mean, label=label, marker=".")
        ax.fill_between(
            x,
            [m - s for m, s in zip(mean, std)],
            [m + s for m, s in zip(mean, std)],
            color="#6b2def",
            alpha=0.2,
        )
    ax.legend(loc=4)

    ax.set_xlabel(x_label_text)
    ax.set_ylabel(y_label_text)

    plt.savefig(graph_path, format="pdf")

    logger.info(f"Comparison graph saved to '{graph_path}'.")


if __name__ == "__main__":
    raise RuntimeError(
        "Calling `rasa.core.test` directly is no longer supported. Please use "
        "`rasa test` to test a combined Core and NLU model or `rasa test core` "
        "to test a Core model."
    )
