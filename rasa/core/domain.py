import collections
import json
import logging
import os
import typing
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Set, Text, Tuple, Union

import rasa.core.constants
from rasa.utils.common import (
    raise_warning,
    lazy_property,
    sort_list_of_dicts_by_first_key,
)
import rasa.utils.io
from rasa.cli.utils import bcolors, wrap_with_color
from rasa.constants import (
    DEFAULT_CARRY_OVER_SLOTS_TO_NEW_SESSION,
    DOMAIN_SCHEMA_FILE,
    DOCS_URL_DOMAINS,
)
from rasa.core import utils
from rasa.core.actions import action  # pytype: disable=pyi-error
from rasa.core.actions.action import Action  # pytype: disable=pyi-error
from rasa.core.constants import (
    DEFAULT_KNOWLEDGE_BASE_ACTION,
    REQUESTED_SLOT,
    SLOT_LAST_OBJECT,
    SLOT_LAST_OBJECT_TYPE,
    SLOT_LISTED_ITEMS,
)
from rasa.core.events import SlotSet, UserUttered
from rasa.core.slots import Slot, UnfeaturizedSlot
from rasa.utils.endpoints import EndpointConfig
from rasa.utils.validation import InvalidYamlFileError, validate_yaml_schema

logger = logging.getLogger(__name__)

PREV_PREFIX = "prev_"
ACTIVE_FORM_PREFIX = "active_form_"

CARRY_OVER_SLOTS_KEY = "carry_over_slots_to_new_session"
SESSION_EXPIRATION_TIME_KEY = "session_expiration_time"
SESSION_CONFIG_KEY = "session_config"

if typing.TYPE_CHECKING:
    from rasa.core.trackers import DialogueStateTracker


class InvalidDomain(Exception):
    """Exception that can be raised when domain is not valid."""

    def __init__(self, message) -> None:
        self.message = message

    def __str__(self):
        # return message in error colours
        return wrap_with_color(self.message, color=bcolors.FAIL)


class SessionConfig(NamedTuple):
    session_expiration_time: float  # in minutes
    carry_over_slots: bool

    @staticmethod
    def default() -> "SessionConfig":
        # TODO: 2.0, reconsider how to apply sessions to old projects
        return SessionConfig(0, DEFAULT_CARRY_OVER_SLOTS_TO_NEW_SESSION)

    def are_sessions_enabled(self) -> bool:
        return self.session_expiration_time > 0


class Domain:
    """The domain specifies the universe in which the bot's policy acts.

    A Domain subclass provides the actions the bot can take, the intents
    and entities it can recognise"""

    @classmethod
    def empty(cls) -> "Domain":
        return cls([], [], [], {}, [], [])

    @classmethod
    def load(cls, paths: Union[List[Text], Text]) -> "Domain":
        if not paths:
            raise InvalidDomain(
                "No domain file was specified. Please specify a path "
                "to a valid domain file."
            )
        elif not isinstance(paths, list) and not isinstance(paths, set):
            paths = [paths]

        domain = Domain.empty()
        for path in paths:
            other = cls.from_path(path)
            domain = domain.merge(other)

        return domain

    @classmethod
    def from_path(cls, path: Text) -> "Domain":
        path = os.path.abspath(path)

        if os.path.isfile(path):
            domain = cls.from_file(path)
        elif os.path.isdir(path):
            domain = cls.from_directory(path)
        else:
            raise InvalidDomain(
                "Failed to load domain specification from '{}'. "
                "File not found!".format(os.path.abspath(path))
            )

        return domain

    @classmethod
    def from_file(cls, path: Text) -> "Domain":
        return cls.from_yaml(rasa.utils.io.read_file(path))

    @classmethod
    def from_yaml(cls, yaml: Text) -> "Domain":
        try:
            validate_yaml_schema(yaml, DOMAIN_SCHEMA_FILE)
        except InvalidYamlFileError as e:
            raise InvalidDomain(str(e))

        data = rasa.utils.io.read_yaml(yaml)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict) -> "Domain":
        utter_templates = cls.collect_templates(data.get("responses", {}))
        if "templates" in data:
            raise_warning(
                "Your domain file contains the key: 'templates'. This has been "
                "deprecated and renamed to 'responses'. The 'templates' key will "
                "no longer work in future versions of Rasa. Please replace "
                "'templates' with 'responses'",
                FutureWarning,
                docs=DOCS_URL_DOMAINS,
            )
            utter_templates = cls.collect_templates(data.get("templates", {}))

        slots = cls.collect_slots(data.get("slots", {}))
        additional_arguments = data.get("config", {})
        session_config = cls._get_session_config(data.get(SESSION_CONFIG_KEY, {}))
        intents = data.get("intents", {})

        return cls(
            intents,
            data.get("entities", []),
            slots,
            utter_templates,
            data.get("actions", []),
            data.get("forms", []),
            session_config=session_config,
            **additional_arguments,
        )

    @staticmethod
    def _get_session_config(session_config: Dict) -> SessionConfig:
        session_expiration_time = session_config.get(SESSION_EXPIRATION_TIME_KEY)

        # TODO: 2.0 reconsider how to apply sessions to old projects and legacy trackers
        if session_expiration_time is None:
            raise_warning(
                "No tracker session configuration was found in the loaded domain. "
                "Domains without a session config will automatically receive a "
                "session expiration time of 60 minutes in Rasa version 2.0 if not "
                "configured otherwise.",
                FutureWarning,
                docs=DOCS_URL_DOMAINS + "#session-configuration",
            )
            session_expiration_time = 0

        carry_over_slots = session_config.get(
            CARRY_OVER_SLOTS_KEY, DEFAULT_CARRY_OVER_SLOTS_TO_NEW_SESSION
        )

        return SessionConfig(session_expiration_time, carry_over_slots)

    @classmethod
    def from_directory(cls, path: Text) -> "Domain":
        """Loads and merges multiple domain files recursively from a directory tree."""
        from rasa import data

        domain = Domain.empty()
        for root, _, files in os.walk(path):
            for file in files:
                full_path = os.path.join(root, file)
                if data.is_domain_file(full_path):
                    other = Domain.from_file(full_path)
                    domain = other.merge(domain)

        return domain

    def merge(self, domain: Optional["Domain"], override: bool = False) -> "Domain":
        """Merge this domain with another one, combining their attributes.

        List attributes like ``intents`` and ``actions`` will be deduped
        and merged. Single attributes will be taken from ``self`` unless
        override is `True`, in which case they are taken from ``domain``."""

        if not domain:
            return self

        domain_dict = domain.as_dict()
        combined = self.as_dict()

        def merge_dicts(
            d1: Dict[Text, Any],
            d2: Dict[Text, Any],
            override_existing_values: bool = False,
        ) -> Dict[Text, Any]:
            if override_existing_values:
                a, b = d1.copy(), d2.copy()
            else:
                a, b = d2.copy(), d1.copy()
            a.update(b)
            return a

        def merge_lists(l1: List[Any], l2: List[Any]) -> List[Any]:
            return sorted(list(set(l1 + l2)))

        if override:
            config = domain_dict["config"]
            for key, val in config.items():  # pytype: disable=attribute-error
                combined["config"][key] = val

        if override or self.session_config == SessionConfig.default():
            combined[SESSION_CONFIG_KEY] = domain_dict[SESSION_CONFIG_KEY]

        # intents is list of dicts
        intents_1 = {list(i.keys())[0]: i for i in combined["intents"]}
        intents_2 = {list(i.keys())[0]: i for i in domain_dict["intents"]}
        merged_intents = merge_dicts(intents_1, intents_2, override)
        combined["intents"] = list(merged_intents.values())

        # remove existing forms from new actions
        for form in combined["forms"]:
            if form in domain_dict["actions"]:
                domain_dict["actions"].remove(form)

        for key in ["entities", "actions", "forms"]:
            combined[key] = merge_lists(combined[key], domain_dict[key])

        for key in ["responses", "slots"]:
            combined[key] = merge_dicts(combined[key], domain_dict[key], override)

        return self.__class__.from_dict(combined)

    @staticmethod
    def collect_slots(slot_dict: Dict[Text, Any]) -> List[Slot]:
        # it is super important to sort the slots here!!!
        # otherwise state ordering is not consistent
        slots = []
        for slot_name in sorted(slot_dict):
            slot_class = Slot.resolve_by_type(slot_dict[slot_name].get("type"))
            if "type" in slot_dict[slot_name]:
                del slot_dict[slot_name]["type"]
            slot = slot_class(slot_name, **slot_dict[slot_name])
            slots.append(slot)
        return slots

    @staticmethod
    def collect_intent_properties(
        intents: List[Union[Text, Dict[Text, Any]]]
    ) -> Dict[Text, Dict[Text, Union[bool, List]]]:
        intent_properties = {}
        for intent in intents:
            if isinstance(intent, dict):
                name = list(intent.keys())[0]
                for properties in intent.values():
                    properties.setdefault("use_entities", True)
                    properties.setdefault("ignore_entities", [])
                    if (
                        properties["use_entities"] is None
                        or properties["use_entities"] is False
                    ):
                        properties["use_entities"] = []
            else:
                name = intent
                intent = {intent: {"use_entities": True, "ignore_entities": []}}

            if name in intent_properties.keys():
                raise InvalidDomain(
                    "Intents are not unique! Found two intents with name '{}'. "
                    "Either rename or remove one of them.".format(name)
                )

            intent_properties.update(intent)
        return intent_properties

    @staticmethod
    def collect_templates(
        yml_templates: Dict[Text, List[Any]]
    ) -> Dict[Text, List[Dict[Text, Any]]]:
        """Go through the templates and make sure they are all in dict format
        """
        templates = {}
        for template_key, template_variations in yml_templates.items():
            validated_variations = []
            if template_variations is None:
                raise InvalidDomain(
                    "Utterance '{}' does not have any defined templates.".format(
                        template_key
                    )
                )

            for t in template_variations:

                # templates should be a dict with options
                if isinstance(t, str):
                    raise_warning(
                        f"Templates should not be strings anymore. "
                        f"Utterance template '{template_key}' should contain "
                        f"either a '- text: ' or a '- custom: ' "
                        f"attribute to be a proper template.",
                        FutureWarning,
                        docs=DOCS_URL_DOMAINS + "#utterance-templates",
                    )
                    validated_variations.append({"text": t})
                elif "text" not in t and "custom" not in t:
                    raise InvalidDomain(
                        f"Utter template '{template_key}' needs to contain either "
                        f"'- text: ' or '- custom: ' attribute to be a proper "
                        f"template."
                    )
                else:
                    validated_variations.append(t)

            templates[template_key] = validated_variations
        return templates

    def __init__(
        self,
        intents: Union[Set[Text], List[Union[Text, Dict[Text, Any]]]],
        entities: List[Text],
        slots: List[Slot],
        templates: Dict[Text, List[Dict[Text, Any]]],
        action_names: List[Text],
        form_names: List[Text],
        store_entities_as_slots: bool = True,
        session_config: SessionConfig = SessionConfig.default(),
    ) -> None:

        self.intent_properties = self.collect_intent_properties(intents)
        self.entities = entities
        self.form_names = form_names
        self.slots = slots
        self.templates = templates
        self.session_config = session_config

        # only includes custom actions and utterance actions
        self.user_actions = action.combine_with_templates(action_names, templates)

        # includes all actions (custom, utterance, default actions and forms)
        self.action_names = (
            action.combine_user_with_default_actions(self.user_actions) + form_names
        )

        self.store_entities_as_slots = store_entities_as_slots
        self._check_domain_sanity()

    def __hash__(self) -> int:

        self_as_dict = self.as_dict()
        self_as_dict["intents"] = sort_list_of_dicts_by_first_key(
            self_as_dict["intents"]
        )
        self_as_string = json.dumps(self_as_dict, sort_keys=True)
        text_hash = utils.get_text_hash(self_as_string)

        return int(text_hash, 16)

    @lazy_property
    def user_actions_and_forms(self):
        """Returns combination of user actions and forms"""

        return self.user_actions + self.form_names

    @lazy_property
    def num_actions(self):
        """Returns the number of available actions."""

        # noinspection PyTypeChecker
        return len(self.action_names)

    @lazy_property
    def num_states(self):
        """Number of used input states for the action prediction."""

        return len(self.input_states)

    def add_requested_slot(self) -> None:
        """Add a slot called `requested_slot` to the list of slots.

        The value of this slot will hold the name of the slot which the user
        needs to fill in next (either explicitly or implicitly) as part of a form.
        """
        if self.form_names and REQUESTED_SLOT not in [s.name for s in self.slots]:
            self.slots.append(UnfeaturizedSlot(REQUESTED_SLOT))

    def add_knowledge_base_slots(self) -> None:
        """
        Add slots for the knowledge base action to the list of slots, if the
        default knowledge base action name is present.

        As soon as the knowledge base action is not experimental anymore, we should
        consider creating a new section in the domain file dedicated to knowledge
        base slots.
        """
        if DEFAULT_KNOWLEDGE_BASE_ACTION in self.action_names:
            logger.warning(
                "You are using an experiential feature: Action '{}'!".format(
                    DEFAULT_KNOWLEDGE_BASE_ACTION
                )
            )
            slot_names = [s.name for s in self.slots]
            knowledge_base_slots = [
                SLOT_LISTED_ITEMS,
                SLOT_LAST_OBJECT,
                SLOT_LAST_OBJECT_TYPE,
            ]
            for s in knowledge_base_slots:
                if s not in slot_names:
                    self.slots.append(UnfeaturizedSlot(s))

    def action_for_name(
        self, action_name: Text, action_endpoint: Optional[EndpointConfig]
    ) -> Optional[Action]:
        """Looks up which action corresponds to this action name."""

        if action_name not in self.action_names:
            self._raise_action_not_found_exception(action_name)

        return action.action_from_name(
            action_name, action_endpoint, self.user_actions_and_forms
        )

    def action_for_index(
        self, index: int, action_endpoint: Optional[EndpointConfig]
    ) -> Optional[Action]:
        """Integer index corresponding to an actions index in the action list.

        This method resolves the index to the actions name."""

        if self.num_actions <= index or index < 0:
            raise IndexError(
                "Cannot access action at index {}. "
                "Domain has {} actions."
                "".format(index, self.num_actions)
            )

        return self.action_for_name(self.action_names[index], action_endpoint)

    def actions(self, action_endpoint) -> List[Optional[Action]]:
        return [
            self.action_for_name(name, action_endpoint) for name in self.action_names
        ]

    def index_for_action(self, action_name: Text) -> Optional[int]:
        """Looks up which action index corresponds to this action name"""

        try:
            return self.action_names.index(action_name)
        except ValueError:
            self._raise_action_not_found_exception(action_name)

    def _raise_action_not_found_exception(self, action_name) -> typing.NoReturn:
        action_names = "\n".join([f"\t - {a}" for a in self.action_names])
        raise NameError(
            f"Cannot access action '{action_name}', "
            f"as that name is not a registered "
            f"action for this domain. "
            f"Available actions are: \n{action_names}"
        )

    def random_template_for(self, utter_action: Text) -> Optional[Dict[Text, Any]]:
        import numpy as np

        if utter_action in self.templates:
            return np.random.choice(self.templates[utter_action])
        else:
            return None

    # noinspection PyTypeChecker
    @lazy_property
    def slot_states(self) -> List[Text]:
        """Returns all available slot state strings."""

        return [
            f"slot_{s.name}_{i}"
            for s in self.slots
            for i in range(0, s.feature_dimensionality())
        ]

    # noinspection PyTypeChecker
    @lazy_property
    def prev_action_states(self) -> List[Text]:
        """Returns all available previous action state strings."""

        return [PREV_PREFIX + a for a in self.action_names]

    # noinspection PyTypeChecker
    @lazy_property
    def intent_states(self) -> List[Text]:
        """Returns all available previous action state strings."""

        return [f"intent_{i}" for i in self.intents]

    # noinspection PyTypeChecker
    @lazy_property
    def entity_states(self) -> List[Text]:
        """Returns all available previous action state strings."""

        return [f"entity_{e}" for e in self.entities]

    # noinspection PyTypeChecker
    @lazy_property
    def form_states(self) -> List[Text]:
        return [f"active_form_{f}" for f in self.form_names]

    def index_of_state(self, state_name: Text) -> Optional[int]:
        """Provides the index of a state."""

        return self.input_state_map.get(state_name)

    @lazy_property
    def input_state_map(self) -> Dict[Text, int]:
        """Provides a mapping from state names to indices."""
        return {f: i for i, f in enumerate(self.input_states)}

    @lazy_property
    def input_states(self) -> List[Text]:
        """Returns all available states."""

        return (
            self.intent_states
            + self.entity_states
            + self.slot_states
            + self.prev_action_states
            + self.form_states
        )

    def get_parsing_states(self, tracker: "DialogueStateTracker") -> Dict[Text, float]:
        state_dict = {}

        # Set all found entities with the state value 1.0, unless they should
        # be ignored for the current intent
        latest_message = tracker.latest_message

        if not latest_message:
            return state_dict

        intent_name = latest_message.intent.get("name")

        if intent_name:
            for entity_name in self._get_featurized_entities(latest_message):
                key = f"entity_{entity_name}"
                state_dict[key] = 1.0

        # Set all set slots with the featurization of the stored value
        for key, slot in tracker.slots.items():
            if slot is not None:
                for i, slot_value in enumerate(slot.as_feature()):
                    if slot_value != 0:
                        slot_id = f"slot_{key}_{i}"
                        state_dict[slot_id] = slot_value

        if "intent_ranking" in latest_message.parse_data:
            for intent in latest_message.parse_data["intent_ranking"]:
                if intent.get("name"):
                    intent_id = "intent_{}".format(intent["name"])
                    state_dict[intent_id] = intent["confidence"]

        elif intent_name:
            intent_id = "intent_{}".format(latest_message.intent["name"])
            state_dict[intent_id] = latest_message.intent.get("confidence", 1.0)

        return state_dict

    def _get_featurized_entities(self, latest_message: UserUttered) -> Set[Text]:
        intent_name = latest_message.intent.get("name")
        intent_config = self.intent_config(intent_name)
        entities = latest_message.entities
        entity_names = {
            entity["entity"] for entity in entities if "entity" in entity.keys()
        }

        # `use_entities` is either a list of explicitly included entities
        # or `True` if all should be included
        include = intent_config.get("use_entities", True)
        included_entities = set(entity_names if include is True else include)
        excluded_entities = set(intent_config.get("ignore_entities", []))
        wanted_entities = included_entities - excluded_entities

        # Only print warning for ambiguous configurations if entities were included
        # explicitly.
        explicitly_included = isinstance(include, list)
        ambiguous_entities = included_entities.intersection(excluded_entities)
        if explicitly_included and ambiguous_entities:
            raise_warning(
                f"Entities: '{ambiguous_entities}' are explicitly included and"
                f" excluded for intent '{intent_name}'."
                f"Excluding takes precedence in this case. "
                f"Please resolve that ambiguity.",
                docs=DOCS_URL_DOMAINS + "#ignoring-entities-for-certain-intents",
            )

        return entity_names.intersection(wanted_entities)

    def get_prev_action_states(
        self, tracker: "DialogueStateTracker"
    ) -> Dict[Text, float]:
        """Turns the previous taken action into a state name."""

        latest_action = tracker.latest_action_name
        if latest_action:
            prev_action_name = PREV_PREFIX + latest_action
            if prev_action_name in self.input_state_map:
                return {prev_action_name: 1.0}
            else:
                return {}
        else:
            return {}

    @staticmethod
    def get_active_form(tracker: "DialogueStateTracker") -> Dict[Text, float]:
        """Turns tracker's active form into a state name."""
        form = tracker.active_form.get("name")
        if form is not None:
            return {ACTIVE_FORM_PREFIX + form: 1.0}
        else:
            return {}

    def get_active_states(self, tracker: "DialogueStateTracker") -> Dict[Text, float]:
        """Return a bag of active states from the tracker state"""
        state_dict = self.get_parsing_states(tracker)
        state_dict.update(self.get_prev_action_states(tracker))
        state_dict.update(self.get_active_form(tracker))
        return state_dict

    def states_for_tracker_history(
        self, tracker: "DialogueStateTracker"
    ) -> List[Dict[Text, float]]:
        """Array of states for each state of the trackers history."""
        return [
            self.get_active_states(tr) for tr in tracker.generate_all_prior_trackers()
        ]

    def slots_for_entities(self, entities: List[Dict[Text, Any]]) -> List[SlotSet]:
        if self.store_entities_as_slots:
            slot_events = []
            for s in self.slots:
                if s.auto_fill:
                    matching_entities = [
                        e["value"] for e in entities if e["entity"] == s.name
                    ]
                    if matching_entities:
                        if s.type_name == "list":
                            slot_events.append(SlotSet(s.name, matching_entities))
                        else:
                            slot_events.append(SlotSet(s.name, matching_entities[-1]))
            return slot_events
        else:
            return []

    def persist_specification(self, model_path: Text) -> None:
        """Persists the domain specification to storage."""

        domain_spec_path = os.path.join(model_path, "domain.json")
        rasa.utils.io.create_directory_for_file(domain_spec_path)

        metadata = {"states": self.input_states}
        rasa.utils.io.dump_obj_as_json_to_file(domain_spec_path, metadata)

    @classmethod
    def load_specification(cls, path: Text) -> Dict[Text, Any]:
        """Load a domains specification from a dumped model directory."""

        metadata_path = os.path.join(path, "domain.json")
        specification = json.loads(rasa.utils.io.read_file(metadata_path))
        return specification

    def compare_with_specification(self, path: Text) -> bool:
        """Compares the domain spec of the current and the loaded domain.

        Throws exception if the loaded domain specification is different
        to the current domain are different."""

        loaded_domain_spec = self.load_specification(path)
        states = loaded_domain_spec["states"]

        if set(states) != set(self.input_states):
            missing = ",".join(set(states) - set(self.input_states))
            additional = ",".join(set(self.input_states) - set(states))
            raise InvalidDomain(
                f"Domain specification has changed. "
                f"You MUST retrain the policy. "
                f"Detected mismatch in domain specification. "
                f"The following states have been \n"
                f"\t - removed: {missing} \n"
                f"\t - added:   {additional} "
            )
        else:
            return True

    def _slot_definitions(self) -> Dict[Any, Dict[str, Any]]:
        return {slot.name: slot.persistence_info() for slot in self.slots}

    def as_dict(self) -> Dict[Text, Any]:

        return {
            "config": {"store_entities_as_slots": self.store_entities_as_slots},
            SESSION_CONFIG_KEY: {
                SESSION_EXPIRATION_TIME_KEY: self.session_config.session_expiration_time,
                CARRY_OVER_SLOTS_KEY: self.session_config.carry_over_slots,
            },
            "intents": [{k: v} for k, v in self.intent_properties.items()],
            "entities": self.entities,
            "slots": self._slot_definitions(),
            "responses": self.templates,
            "actions": self.user_actions,  # class names of the actions
            "forms": self.form_names,
        }

    def persist(self, filename: Union[Text, Path]) -> None:
        """Write domain to a file."""

        domain_data = self.as_dict()
        utils.dump_obj_as_yaml_to_file(filename, domain_data)

    def cleaned_domain(self) -> Dict[Text, Any]:
        """Fetch cleaned domain, replacing redundant keys with default values."""

        domain_data = self.as_dict()
        for idx, intent_info in enumerate(domain_data["intents"]):
            for name, intent in intent_info.items():
                if intent.get("use_entities") is True:
                    intent.pop("use_entities")
                if not intent.get("ignore_entities"):
                    intent.pop("ignore_entities", None)
                if len(intent) == 0:
                    domain_data["intents"][idx] = name

        for slot in domain_data["slots"].values():  # pytype: disable=attribute-error
            if slot["initial_value"] is None:
                del slot["initial_value"]
            if slot["auto_fill"]:
                del slot["auto_fill"]
            if slot["type"].startswith("rasa.core.slots"):
                slot["type"] = Slot.resolve_by_type(slot["type"]).type_name

        if domain_data["config"]["store_entities_as_slots"]:
            del domain_data["config"]["store_entities_as_slots"]

        # clean empty keys
        return {
            k: v
            for k, v in domain_data.items()
            if v != {} and v != [] and v is not None
        }

    def persist_clean(self, filename: Text) -> None:
        """Write cleaned domain to a file."""

        cleaned_domain_data = self.cleaned_domain()
        utils.dump_obj_as_yaml_to_file(filename, cleaned_domain_data)

    def as_yaml(self, clean_before_dump: bool = False) -> Text:
        if clean_before_dump:
            domain_data = self.cleaned_domain()
        else:
            domain_data = self.as_dict()

        return utils.dump_obj_as_yaml_to_string(domain_data)

    def intent_config(self, intent_name: Text) -> Dict[Text, Any]:
        """Return the configuration for an intent."""
        return self.intent_properties.get(intent_name, {})

    @lazy_property
    def intents(self):
        return sorted(self.intent_properties.keys())

    @property
    def _slots_for_domain_warnings(self) -> List[Text]:
        """Fetch names of slots that are used in domain warnings.

        Excludes slots of type `UnfeaturizedSlot`.
        """

        return [s.name for s in self.slots if not isinstance(s, UnfeaturizedSlot)]

    @property
    def _actions_for_domain_warnings(self) -> List[Text]:
        """Fetch names of actions that are used in domain warnings.

        Includes user and form actions, but excludes those that are default actions.
        """

        from rasa.core.actions.action import (  # pytype: disable=pyi-error
            default_action_names,
        )

        return [
            a for a in self.user_actions_and_forms if a not in default_action_names()
        ]

    @staticmethod
    def _get_symmetric_difference(
        domain_elements: Union[List[Text], Set[Text]],
        training_data_elements: Optional[Union[List[Text], Set[Text]]],
    ) -> Dict[Text, Set[Text]]:
        """Get symmetric difference between a set of domain elements and a set of
        training data elements.

        Returns a dictionary containing a list of items found in the `domain_elements`
        but not in `training_data_elements` at key `in_domain`, and a list of items
        found in `training_data_elements` but not in `domain_elements` at key
        `in_training_data_set`.
        """

        if training_data_elements is None:
            training_data_elements = set()

        in_domain_diff = set(domain_elements) - set(training_data_elements)
        in_training_data_diff = set(training_data_elements) - set(domain_elements)

        return {"in_domain": in_domain_diff, "in_training_data": in_training_data_diff}

    def domain_warnings(
        self,
        intents: Optional[Union[List[Text], Set[Text]]] = None,
        entities: Optional[Union[List[Text], Set[Text]]] = None,
        actions: Optional[Union[List[Text], Set[Text]]] = None,
        slots: Optional[Union[List[Text], Set[Text]]] = None,
    ) -> Dict[Text, Dict[Text, Set[Text]]]:
        """Generate domain warnings from intents, entities, actions and slots.

        Returns a dictionary with entries for `intent_warnings`,
        `entity_warnings`, `action_warnings` and `slot_warnings`. Excludes domain slots
        of type `UnfeaturizedSlot` from domain warnings.
        """

        intent_warnings = self._get_symmetric_difference(self.intents, intents)
        entity_warnings = self._get_symmetric_difference(self.entities, entities)
        action_warnings = self._get_symmetric_difference(
            self._actions_for_domain_warnings, actions
        )
        slot_warnings = self._get_symmetric_difference(
            self._slots_for_domain_warnings, slots
        )

        return {
            "intent_warnings": intent_warnings,
            "entity_warnings": entity_warnings,
            "action_warnings": action_warnings,
            "slot_warnings": slot_warnings,
        }

    def _check_domain_sanity(self) -> None:
        """Make sure the domain is properly configured.
        If the domain contains any duplicate slots, intents, actions
        or entities, an InvalidDomain error is raised.  This error
        is also raised when intent-action mappings are incorrectly
        named or an utterance template is missing."""

        def get_duplicates(my_items):
            """Returns a list of duplicate items in my_items."""

            return [
                item
                for item, count in collections.Counter(my_items).items()
                if count > 1
            ]

        def check_mappings(
            intent_properties: Dict[Text, Dict[Text, Union[bool, List]]]
        ) -> List[Tuple[Text, Text]]:
            """Check whether intent-action mappings use proper action names."""

            incorrect = list()
            for intent, properties in intent_properties.items():
                if "triggers" in properties:
                    triggered_action = properties.get("triggers")
                    if triggered_action not in self.action_names:
                        incorrect.append((intent, str(triggered_action)))
            return incorrect

        def get_exception_message(
            duplicates: Optional[List[Tuple[List[Text], Text]]] = None,
            mappings: List[Tuple[Text, Text]] = None,
        ):
            """Return a message given a list of error locations."""

            message = ""
            if duplicates:
                message += get_duplicate_exception_message(duplicates)
            if mappings:
                if message:
                    message += "\n"
                message += get_mapping_exception_message(mappings)
            return message

        def get_mapping_exception_message(mappings: List[Tuple[Text, Text]]):
            """Return a message given a list of duplicates."""

            message = ""
            for name, action_name in mappings:
                if message:
                    message += "\n"
                message += (
                    "Intent '{}' is set to trigger action '{}', which is "
                    "not defined in the domain.".format(name, action_name)
                )
            return message

        def get_duplicate_exception_message(
            duplicates: List[Tuple[List[Text], Text]]
        ) -> Text:
            """Return a message given a list of duplicates."""

            message = ""
            for d, name in duplicates:
                if d:
                    if message:
                        message += "\n"
                    message += (
                        f"Duplicate {name} in domain. "
                        f"These {name} occur more than once in "
                        f"the domain: '{', '.join(d)}'."
                    )
            return message

        duplicate_actions = get_duplicates(self.action_names)
        duplicate_slots = get_duplicates([s.name for s in self.slots])
        duplicate_entities = get_duplicates(self.entities)
        incorrect_mappings = check_mappings(self.intent_properties)

        if (
            duplicate_actions
            or duplicate_slots
            or duplicate_entities
            or incorrect_mappings
        ):
            raise InvalidDomain(
                get_exception_message(
                    [
                        (duplicate_actions, "actions"),
                        (duplicate_slots, "slots"),
                        (duplicate_entities, "entities"),
                    ],
                    incorrect_mappings,
                )
            )

    def check_missing_templates(self) -> None:
        """Warn user of utterance names which have no specified template."""

        utterances = [
            a
            for a in self.action_names
            if a.startswith(rasa.core.constants.UTTER_PREFIX)
        ]

        missing_templates = [t for t in utterances if t not in self.templates.keys()]

        if missing_templates:
            for template in missing_templates:
                raise_warning(
                    f"Utterance '{template}' is listed as an "
                    f"action in the domain file, but there is "
                    f"no matching utterance template. Please "
                    f"check your domain.",
                    docs=DOCS_URL_DOMAINS + "#utterance-templates",
                )

    def is_empty(self) -> bool:
        """Checks whether the domain is empty."""

        return self.as_dict() == Domain.empty().as_dict()


class TemplateDomain(Domain):
    pass
