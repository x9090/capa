# Copyright (C) 2020 FireEye, Inc. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
# You may obtain a copy of the License at: [package root]/LICENSE.txt
# Unless required by applicable law or agreed to in writing, software distributed under the License
#  is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and limitations under the License.

import os
import sys
import json
import types
import copy
import logging
import itertools
import collections

import idc
import idaapi
import ida_kernwin
import ida_settings
from PyQt5 import QtGui, QtCore, QtWidgets

import capa.main
import capa.rules
import capa.ida.helpers
import capa.render.utils as rutils
import capa.features.extractors.ida
from capa.ida.plugin.icon import QICON
from capa.ida.plugin.view import (
    CapaExplorerQtreeView,
    CapaExplorerRulegenFeatures,
    CapaExplorerRulgenEditor,
    CapaExplorerRulgenPreview,
)
from capa.ida.plugin.hooks import CapaExplorerIdaHooks
from capa.ida.plugin.model import CapaExplorerDataModel
from capa.ida.plugin.proxy import CapaExplorerRangeProxyModel, CapaExplorerSearchProxyModel

logger = logging.getLogger(__name__)
settings = ida_settings.IDASettings("capa")


def write_file(path, data):
    """ """
    if os.path.exists(path) and 1 != idaapi.ask_yn(1, "The file already exists. Overwrite?"):
        return
    with open(path, "wb") as save_file:
        save_file.write(data)


def trim_function_name(f, max_length=25):
    """ """
    n = idaapi.get_name(f.start_ea)
    if len(n) > max_length:
        n = "%s..." % n[:max_length]
    return n


def trim_scope(n):
    """ """
    if "/" in n:
        n = n.rpartition("/")[0]
    return n


def find_func_features(f, extractor):
    """ """
    func_features = collections.defaultdict(set)
    bb_features = collections.defaultdict(dict)

    for (feature, ea) in extractor.extract_function_features(f):
        func_features[feature].add(ea)

    for bb in extractor.get_basic_blocks(f):
        _bb_features = collections.defaultdict(set)

        for (feature, ea) in extractor.extract_basic_block_features(f, bb):
            _bb_features[feature].add(ea)
            func_features[feature].add(ea)

        for insn in extractor.get_instructions(f, bb):
            for (feature, ea) in extractor.extract_insn_features(f, bb, insn):
                _bb_features[feature].add(ea)
                func_features[feature].add(ea)

        bb_features[capa.helpers.oint(bb)] = _bb_features

    return func_features, bb_features


def find_func_matches(f, ruleset, func_features, bb_features):
    """ """
    func_matches = collections.defaultdict(list)
    bb_matches = collections.defaultdict(list)

    # create copy of function features, to add rule matches for basic blocks
    func_features = collections.defaultdict(set, copy.deepcopy(func_features))

    # find rule matches for basic blocks
    for (bb, features) in bb_features.items():
        _, matches = capa.engine.match(ruleset.basic_block_rules, features, bb)
        for (name, res) in matches.items():
            name = trim_scope(name)
            bb_matches[name].extend(res)
            for (ea, _) in res:
                func_features[capa.features.MatchedRule(name)].add(ea)

    # find rule matches for function, function features include rule matches for basic blocks
    _, matches = capa.engine.match(ruleset.function_rules, func_features, capa.helpers.oint(f))
    for (name, res) in matches.items():
        name = trim_scope(name)
        func_matches[name].extend(res)

    return func_matches, bb_matches


def update_wait_box(text):
    """update the IDA wait box"""
    ida_kernwin.replace_wait_box("capa explorer...%s" % text)


class UserCancelledError(Exception):
    """throw exception when user cancels action"""

    pass


class CapaExplorerProgressIndicator(QtCore.QObject):
    """implement progress signal, used during feature extraction"""

    progress = QtCore.pyqtSignal(str)

    def __init__(self):
        """initialize signal object"""
        super(CapaExplorerProgressIndicator, self).__init__()

    def update(self, text):
        """emit progress update

        check if user cancelled action, raise exception for parent function to catch
        """
        if ida_kernwin.user_cancelled():
            raise UserCancelledError("user cancelled")
        self.progress.emit("extracting features from %s" % text)


class CapaExplorerFeatureExtractor(capa.features.extractors.ida.IdaFeatureExtractor):
    """subclass the IdaFeatureExtractor

    track progress during feature extraction, also allow user to cancel feature extraction
    """

    def __init__(self):
        super(CapaExplorerFeatureExtractor, self).__init__()
        self.indicator = CapaExplorerProgressIndicator()

    def extract_function_features(self, f):
        self.indicator.update("function at 0x%X" % f.start_ea)
        return super(CapaExplorerFeatureExtractor, self).extract_function_features(f)


class CapaExplorerForm(idaapi.PluginForm):
    """form element for plugin interface"""

    def __init__(self, name):
        """initialize form elements"""
        super(CapaExplorerForm, self).__init__()

        self.form_title = name
        self.process_total = 0
        self.process_count = 0

        self.parent = None
        self.ida_hooks = None
        self.doc = None

        # models
        self.model_data = None
        self.range_model_proxy = None
        self.search_model_proxy = None

        # UI controls
        self.view_limit_results_by_function = None
        self.view_search_bar = None
        self.view_tree = None
        self.view_rulegen = None
        self.view_tabs = None
        self.view_tab_rulegen = None
        self.view_menu_bar = None
        self.view_status_label = None
        self.view_buttons = None
        self.view_analyze_button = None
        self.view_reset_button = None
        self.view_save_button = None

        self.view_rulegen_preview = None
        self.view_rulegen_features = None
        self.view_rulegen_editor = None
        self.view_rulegen_header_label = None
        self.view_rulegen_search = None
        self.rulegen_rules = None
        self.rulegen_current_function = None
        self.rulegen_bb_features_cache = None
        self.rulegen_func_features_cache = None
        self.view_rulegen_status_label = None

        self.Show()

    def OnCreate(self, form):
        """called when plugin form is created

        load interface and install hooks but do not analyze database
        """
        self.parent = self.FormToPyQtWidget(form)
        self.parent.setWindowIcon(QICON)

        self.load_interface()
        self.load_ida_hooks()

    def Show(self):
        """creates form if not already create, else brings plugin to front"""
        return super(CapaExplorerForm, self).Show(
            self.form_title,
            options=(
                idaapi.PluginForm.WOPN_TAB
                | idaapi.PluginForm.WOPN_RESTORE
                | idaapi.PluginForm.WCLS_CLOSE_LATER
                | idaapi.PluginForm.WCLS_SAVE
            ),
        )

    def OnClose(self, form):
        """called when form is closed

        ensure any plugin modifications (e.g. hooks and UI changes) are reset before the plugin is closed
        """
        self.unload_ida_hooks()
        self.model_data.reset()

    def load_interface(self):
        """load user interface"""
        # load models
        self.model_data = CapaExplorerDataModel()

        # model <- filter range <- filter search <- view

        self.range_model_proxy = CapaExplorerRangeProxyModel()
        self.range_model_proxy.setSourceModel(self.model_data)

        self.search_model_proxy = CapaExplorerSearchProxyModel()
        self.search_model_proxy.setSourceModel(self.range_model_proxy)

        self.view_tree = CapaExplorerQtreeView(self.search_model_proxy, self.parent)

        # load parent tab and children tab views
        self.load_view_tabs()
        self.load_view_checkbox_limit_by()
        self.load_view_search_bar()
        self.load_view_tree_tab()
        self.load_view_rulegen_tab()
        self.load_view_status_label()
        self.load_view_buttons()

        # load menu bar and sub menus
        self.load_view_menu_bar()
        self.load_configure_menu()

        # load parent view
        self.load_view_parent()

    def load_view_tabs(self):
        """load tabs"""
        tabs = QtWidgets.QTabWidget()
        self.view_tabs = tabs

    def load_view_menu_bar(self):
        """load menu bar"""
        bar = QtWidgets.QMenuBar()
        self.view_menu_bar = bar

    def load_view_checkbox_limit_by(self):
        """load limit results by function checkbox"""
        check = QtWidgets.QCheckBox("Limit results to current function")
        check.setChecked(False)
        check.stateChanged.connect(self.slot_checkbox_limit_by_changed)

        self.view_limit_results_by_function = check

    def load_view_status_label(self):
        """load status label"""
        label = QtWidgets.QLabel()
        label.setAlignment(QtCore.Qt.AlignLeft)
        label.setText("Click Analyze to get started...")

        self.view_status_label = label

    def load_view_buttons(self):
        """load the button controls"""
        analyze_button = QtWidgets.QPushButton("Analyze")
        reset_button = QtWidgets.QPushButton("Reset")
        save_button = QtWidgets.QPushButton("Save")

        analyze_button.clicked.connect(self.slot_analyze)
        reset_button.clicked.connect(self.slot_reset)
        save_button.clicked.connect(self.slot_save)

        layout = QtWidgets.QHBoxLayout()
        layout.addWidget(analyze_button)
        layout.addWidget(reset_button)
        layout.addWidget(save_button)
        layout.addStretch(1)

        self.view_analyze_button = analyze_button
        self.view_reset_button = reset_button
        self.view_save_button = save_button
        self.view_buttons = layout

    def load_view_search_bar(self):
        """load the search bar control"""
        line = QtWidgets.QLineEdit()
        line.setPlaceholderText("search...")
        line.textChanged.connect(self.slot_limit_results_to_search)

        self.view_search_bar = line

    def load_view_parent(self):
        """load view parent"""
        layout = QtWidgets.QVBoxLayout()

        layout.addWidget(self.view_tabs)
        layout.addWidget(self.view_status_label)
        layout.addLayout(self.view_buttons)
        layout.setMenuBar(self.view_menu_bar)

        self.parent.setLayout(layout)

    def load_view_tree_tab(self):
        """load tree view tab"""
        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.view_limit_results_by_function)
        layout.addWidget(self.view_search_bar)
        layout.addWidget(self.view_tree)

        tab = QtWidgets.QWidget()
        tab.setLayout(layout)

        self.view_tabs.addTab(tab, "Program Analysis")

    def load_view_rulegen_tab(self):
        """ """
        layout = QtWidgets.QHBoxLayout()
        layout1 = QtWidgets.QVBoxLayout()
        layout2 = QtWidgets.QVBoxLayout()

        right = QtWidgets.QWidget()
        right.setLayout(layout1)

        left = QtWidgets.QWidget()
        left.setLayout(layout2)

        font = QtGui.QFont()
        font.setBold(True)
        font.setPointSize(11)

        label1 = QtWidgets.QLabel()
        label1.setAlignment(QtCore.Qt.AlignLeft)
        label1.setText("Preview")
        label1.setFont(font)

        label2 = QtWidgets.QLabel()
        label2.setAlignment(QtCore.Qt.AlignLeft)
        label2.setText("Editor")
        label2.setFont(font)

        self.view_rulegen_status_label = QtWidgets.QLabel()
        self.view_rulegen_status_label.setAlignment(QtCore.Qt.AlignLeft)
        self.view_rulegen_status_label.setText("")

        self.view_rulegen_search = QtWidgets.QLineEdit()
        self.view_rulegen_search.setPlaceholderText("search...")
        self.view_rulegen_search.setClearButtonEnabled(True)
        self.view_rulegen_search.textChanged.connect(self.slot_limit_rulegen_features_to_search)

        self.view_rulegen_header_label = QtWidgets.QLabel()
        self.view_rulegen_header_label.setAlignment(QtCore.Qt.AlignLeft)
        self.view_rulegen_header_label.setText("Function Features")
        self.view_rulegen_header_label.setFont(font)

        self.view_rulegen_preview = CapaExplorerRulgenPreview(parent=self.parent)
        self.view_rulegen_editor = CapaExplorerRulgenEditor(self.view_rulegen_preview, parent=self.parent)
        self.view_rulegen_features = CapaExplorerRulegenFeatures(self.view_rulegen_editor, parent=self.parent)

        self.view_rulegen_preview.textChanged.connect(self.slot_rulegen_preview_update)

        self.set_rulegen_preview_border_neutral()

        layout1.addWidget(label1)
        layout1.addWidget(self.view_rulegen_preview, 45)
        layout1.addWidget(self.view_rulegen_status_label)
        layout1.addWidget(label2)
        layout1.addWidget(self.view_rulegen_editor, 65)

        layout2.addWidget(self.view_rulegen_header_label)
        layout2.addWidget(self.view_rulegen_search)
        layout2.addWidget(self.view_rulegen_features)

        layout.addWidget(left, 40)
        layout.addWidget(right, 60)

        tab = QtWidgets.QWidget()
        tab.setLayout(layout)

        self.view_tabs.addTab(tab, "Rule Generator")

    def load_configure_menu(self):
        """ """
        actions = (
            ("Change default rules directory...", "Set default rules directory", self.slot_change_rules_dir),
            ("Change default rule author...", "Set default rule author", self.slot_change_rule_author),
            ("Change default rule scope...", "Set default rule scope", self.slot_change_rule_scope),
        )
        self.load_menu("Configuration", actions)

    def load_menu(self, title, actions):
        """load menu actions

        @param title: menu name displayed in UI
        @param actions: tuple of tuples containing action name, tooltip, and slot function
        """
        menu = self.view_menu_bar.addMenu(title)
        for (name, _, slot) in actions:
            action = QtWidgets.QAction(name, self.parent)
            action.triggered.connect(slot)
            menu.addAction(action)

    def load_ida_hooks(self):
        """load IDA UI hooks"""
        # map named action (defined in idagui.cfg) to Python function
        action_hooks = {
            "MakeName": self.ida_hook_rename,
            "EditFunction": self.ida_hook_rename,
            "RebaseProgram": self.ida_hook_rebase,
        }

        self.ida_hooks = CapaExplorerIdaHooks(self.ida_hook_screen_ea_changed, action_hooks)
        self.ida_hooks.hook()

    def unload_ida_hooks(self):
        """unload IDA Pro UI hooks

        must be called before plugin is completely destroyed
        """
        if self.ida_hooks:
            self.ida_hooks.unhook()

    def ida_hook_rename(self, meta, post=False):
        """function hook for IDA "MakeName" and "EditFunction" actions

        called twice, once before action and once after action completes

        @param meta: dict of key/value pairs set when action first called (may be empty)
        @param post: False if action first call, True if action second call
        """
        location = idaapi.get_screen_ea()
        if not location or not capa.ida.helpers.is_func_start(location):
            return

        curr_name = idaapi.get_name(location)

        if post:
            # post action update data model w/ current name
            self.model_data.update_function_name(meta.get("prev_name", ""), curr_name)
        else:
            # pre action so save current name for replacement later
            meta["prev_name"] = curr_name

    def update_view_tree_limit_results_to_function(self, ea):
        """ """
        self.limit_results_to_function(idaapi.get_func(ea))
        self.view_tree.reset_ui()

    def ida_hook_screen_ea_changed(self, widget, new_ea, old_ea):
        """function hook for IDA "screen ea changed" action

        called twice, once before action and once after action completes. this hook is currently only relevant
        for limiting results displayed in the UI

        @param widget: IDA widget type
        @param new_ea: destination ea
        @param old_ea: source ea
        """
        if not self.view_tabs.currentIndex() in (0, 1):
            return

        if idaapi.get_widget_type(widget) != idaapi.BWN_DISASM:
            # ignore views not the assembly view
            return

        if not idaapi.get_func(new_ea):
            return

        if idaapi.get_func(new_ea) == idaapi.get_func(old_ea):
            # user navigated same function - ignore
            return

        if self.view_tabs.currentIndex() == 0 and self.view_limit_results_by_function.isChecked():
            return self.update_view_tree_limit_results_to_function(new_ea)

    def ida_hook_rebase(self, meta, post=False):
        """function hook for IDA "RebaseProgram" action

        called twice, once before action and once after action completes

        @param meta: dict of key/value pairs set when action first called (may be empty)
        @param post: False if action first call, True if action second call
        """
        if post:
            if idaapi.get_imagebase() != meta.get("prev_base", -1):
                capa.ida.helpers.inform_user_ida_ui("Running capa analysis again after program rebase")
                self.slot_analyze()
        else:
            meta["prev_base"] = idaapi.get_imagebase()
            self.model_data.reset()

    def load_capa_rules(self):
        """ """
        try:
            # resolve rules directory - check self and settings first, then ask user
            if not os.path.exists(settings.user.get("rule_path", "")):
                idaapi.info("Please select a file directory containing capa rules.")
                path = self.ask_user_directory()
                if not path:
                    logger.warning(
                        "You must select a file directory containing capa rules before analysis can be run. The standard collection of capa rules can be downloaded from https://github.com/fireeye/capa-rules."
                    )
                    return ()
                settings.user["rule_path"] = path
        except Exception as e:
            logger.error("Failed to load capa rules (error: %s).", e)
            return ()

        if ida_kernwin.user_cancelled():
            logger.info("User cancelled analysis.")
            return ()

        rule_path = settings.user["rule_path"]
        try:
            if not os.path.exists(rule_path):
                raise IOError("rule path %s does not exist or cannot be accessed" % rule_path)

            rule_paths = []
            if os.path.isfile(rule_path):
                rule_paths.append(rule_path)
            elif os.path.isdir(rule_path):
                for root, dirs, files in os.walk(rule_path):
                    if ".github" in root:
                        # the .github directory contains CI config in capa-rules
                        # this includes some .yml files
                        # these are not rules
                        continue
                    for file in files:
                        if not file.endswith(".yml"):
                            if not (file.endswith(".md") or file.endswith(".git") or file.endswith(".txt")):
                                # expect to see readme.md, format.md, and maybe a .git directory
                                # other things maybe are rules, but are mis-named.
                                logger.warning("skipping non-.yml file: %s", file)
                            continue
                        rule_path = os.path.join(root, file)
                        rule_paths.append(rule_path)

            rules = []
            total_paths = len(rule_paths)
            for (i, rule_path) in enumerate(rule_paths):
                update_wait_box(
                    "loading capa rules from %s (%d of %d)" % (settings.user["rule_path"], i + 1, total_paths)
                )
                if ida_kernwin.user_cancelled():
                    raise UserCancelledError("user cancelled")
                try:
                    rule = capa.rules.Rule.from_yaml_file(rule_path)
                except capa.rules.InvalidRule:
                    raise
                else:
                    rule.meta["capa/path"] = rule_path
                    if capa.main.is_nursery_rule_path(rule_path):
                        rule.meta["capa/nursery"] = True
                    rules.append(rule)
            _rules = copy.deepcopy(rules)
            ruleset = capa.rules.RuleSet(_rules)
        except UserCancelledError:
            logger.info("User cancelled analysis.")
            return ()
        except Exception as e:
            capa.ida.helpers.inform_user_ida_ui("Failed to load capa rules from %s" % settings.user["rule_path"])
            logger.error("Failed to load rules from %s (error: %s).", settings.user["rule_path"], e)
            logger.error(
                "Make sure your file directory contains properly formatted capa rules. You can download the standard collection of capa rules from https://github.com/fireeye/capa-rules."
            )
            settings.user["rule_path"] = ""
            return ()

        return ruleset, rules

    def load_capa_results(self):
        """run capa analysis and render results in UI

        note: this function must always return, exception or not, in order for plugin to safely close the IDA
        wait box
        """
        # new analysis, new doc
        self.doc = None
        self.process_total = 0
        self.process_count = 1

        def slot_progress_feature_extraction(text):
            """slot function to handle feature extraction progress updates"""
            update_wait_box("%s (%d of %d)" % (text, self.process_count, self.process_total))
            self.process_count += 1

        extractor = CapaExplorerFeatureExtractor()
        extractor.indicator.progress.connect(slot_progress_feature_extraction)

        update_wait_box("calculating analysis")

        try:
            self.process_total += len(tuple(extractor.get_functions()))
        except Exception as e:
            logger.error("Failed to calculate analysis (error: %s).", e)
            return False

        if ida_kernwin.user_cancelled():
            logger.info("User cancelled analysis.")
            return False

        update_wait_box("loading rules")

        results = self.load_capa_rules()
        if not results:
            return False
        ruleset, rules = results

        if ida_kernwin.user_cancelled():
            logger.info("User cancelled analysis.")
            return False

        update_wait_box("extracting features")

        try:
            meta = capa.ida.helpers.collect_metadata()
            capabilities, counts = capa.main.find_capabilities(ruleset, extractor, disable_progress=True)
            meta["analysis"].update(counts)
        except UserCancelledError:
            logger.info("User cancelled analysis.")
            return False
        except Exception as e:
            logger.error("Failed to extract capabilities from database (error: %s)", e)
            return False

        update_wait_box("checking for file limitations")

        try:
            # support binary files specifically for x86/AMD64 shellcode
            # warn user binary file is loaded but still allow capa to process it
            # TODO: check specific architecture of binary files based on how user configured IDA processors
            if idaapi.get_file_type_name() == "Binary file":
                logger.warning("-" * 80)
                logger.warning(" Input file appears to be a binary file.")
                logger.warning(" ")
                logger.warning(
                    " capa currently only supports analyzing binary files containing x86/AMD64 shellcode with IDA."
                )
                logger.warning(
                    " This means the results may be misleading or incomplete if the binary file loaded in IDA is not x86/AMD64."
                )
                logger.warning(
                    " If you don't know the input file type, you can try using the `file` utility to guess it."
                )
                logger.warning("-" * 80)

                capa.ida.helpers.inform_user_ida_ui("capa encountered file type warnings during analysis")

            if capa.main.has_file_limitation(ruleset, capabilities, is_standalone=False):
                capa.ida.helpers.inform_user_ida_ui("capa encountered file limitation warnings during analysis")
        except Exception as e:
            logger.error("Failed to check for file limitations (error: %s)", e)
            return False

        if ida_kernwin.user_cancelled():
            logger.info("User cancelled analysis.")
            return False

        update_wait_box("rendering results")

        try:
            self.doc = capa.render.convert_capabilities_to_result_document(meta, ruleset, capabilities)
            self.model_data.render_capa_doc(self.doc)
            self.set_view_status_label("capa rules directory: %s (%d rules)" % (settings.user["rule_path"], len(rules)))
        except Exception as e:
            logger.error("Failed to render results (error: %s)", e)
            return False

        return True

    def reset_view_tree(self):
        """reset tree view UI controls

        called when user selects plugin reset from menu
        """
        self.view_limit_results_by_function.setChecked(False)
        self.view_search_bar.setText("")
        self.view_tree.reset_ui()

    def analyze_program(self):
        """ """
        self.range_model_proxy.invalidate()
        self.search_model_proxy.invalidate()
        self.model_data.reset()
        self.model_data.clear()
        self.set_view_status_label("Loading...")

        ida_kernwin.show_wait_box("capa explorer")
        success = self.load_capa_results()
        ida_kernwin.hide_wait_box()

        self.reset_view_tree()

        if not success:
            self.set_view_status_label("Click Analyze to get started...")
            logger.info("Analysis failed.")
        else:
            logger.info("Analysis completed.")

    def analyze_function(self):
        """ """
        self.reset_function_analysis_views()
        self.set_view_status_label("Loading...")

        self.rulegen_current_function = idaapi.get_func(idaapi.get_screen_ea())

        if not self.rulegen_current_function:
            capa.ida.helpers.inform_user_ida_ui("Invalid function")
            self.set_view_status_label("Click Analyze to get started...")
            logger.info(
                "Please navigate to a valid function in the IDA disassembly view before starting function analysis."
            )
            return

        ida_kernwin.show_wait_box("capa explorer")
        results = self.load_capa_rules()
        ida_kernwin.hide_wait_box()

        if not results:
            self.set_view_status_label("Click Analyze to get started...")
            logger.info("Analysis failed.")
            return

        ruleset, self.rulegen_rules = results

        # must use extractor to get function, as capa analysis requires casted object
        extractor = capa.features.extractors.ida.IdaFeatureExtractor()
        f = extractor.get_function(self.rulegen_current_function.start_ea)

        # cache current function for use elsewhere
        self.rulegen_current_function = f

        func_features, bb_features = find_func_features(f, extractor)
        func_matches, bb_matches = find_func_matches(f, ruleset, func_features, bb_features)

        # cache features for use elsewhere
        self.rulegen_func_features_cache = collections.defaultdict(set, copy.deepcopy(func_features))
        self.rulegen_bb_features_cache = collections.defaultdict(dict, copy.deepcopy(bb_features))

        # add function and bb rule matches to function features, for display purposes
        for (name, res) in itertools.chain(func_matches.items(), bb_matches.items()):
            for (ea, _) in res:
                func_features[capa.features.MatchedRule(name)].add(ea)

        # load preview and feature tree
        self.view_rulegen_preview.load_preview_meta(
            f.start_ea,
            settings.user.get("rulegen_author", "<insert_author>"),
            settings.user.get("rulegen_scope", "function"),
        )
        self.view_rulegen_features.load_features(func_features)

        self.view_rulegen_header_label.setText("Function Features (%s)" % trim_function_name(f))
        self.set_view_status_label(
            "capa rules directory: %s (%d rules)" % (settings.user["rule_path"], len(self.rulegen_rules))
        )

        logger.info("Analysis completed.")

    def reset_program_analysis_views(self):
        """ """
        logger.info("Resetting program analysis views.")

        self.model_data.reset()
        self.reset_view_tree()

        logger.info("Reset completed.")

    def reset_function_analysis_views(self):
        """ """
        logger.info("Resetting rule generator views.")

        self.view_rulegen_header_label.setText("Function Features")
        self.view_rulegen_features.reset_view()
        self.view_rulegen_editor.reset_view()
        self.view_rulegen_preview.reset_view()
        self.view_rulegen_search.clear()
        self.set_rulegen_preview_border_neutral()
        self.rulegen_current_function = None
        self.rulegen_rules = None
        self.rulegen_func_features_cache = None
        self.rulegen_bb_features_cache = None
        self.view_rulegen_status_label.clear()

        logger.info("Reset completed.")

    def set_rulegen_status(self, e):
        """ """
        self.view_rulegen_status_label.setText(e)

    def set_rulegen_preview_border_error(self):
        """ """
        self.view_rulegen_preview.setStyleSheet("border: 3px solid red")

    def set_rulegen_preview_border_neutral(self):
        """ """
        self.view_rulegen_preview.setStyleSheet("border: 3px solid grey")

    def set_rulegen_preview_border_warn(self):
        """ """
        self.view_rulegen_preview.setStyleSheet("border: 3px solid yellow")

    def set_rulegen_preview_border_success(self):
        """ """
        self.view_rulegen_preview.setStyleSheet("border: 3px solid green")

    def slot_rulegen_preview_update(self):
        """ """
        if not self.view_rulegen_editor.root:
            self.set_rulegen_preview_border_neutral()
            self.view_rulegen_status_label.clear()
            return

        self.set_rulegen_preview_border_error()

        try:
            rule = capa.rules.Rule.from_yaml(self.view_rulegen_preview.toPlainText())
        except Exception as e:
            self.set_rulegen_status("Failed to compile rule! %s" % e)
            return

        # create deep copy of current rules, add our new rule
        rules = copy.deepcopy(self.rulegen_rules)
        rules.append(rule)

        try:
            func_matches, bb_matches = find_func_matches(
                self.rulegen_current_function,
                capa.rules.RuleSet(list(capa.rules.get_rules_and_dependencies(rules, rule.name))),
                self.rulegen_func_features_cache,
                self.rulegen_bb_features_cache,
            )
        except Exception as e:
            self.set_rulegen_status("Failed to match rule! %s" % e)
            return

        if tuple(filter(lambda m: m[0] == rule.name, itertools.chain(func_matches.items(), bb_matches.items()))):
            # made it here, rule compiled and match was found
            self.set_rulegen_preview_border_success()
            self.set_rulegen_status(
                "Rule compiled, match found for %s" % idaapi.get_name(self.rulegen_current_function.start_ea)
            )
        else:
            # made it here, rule compiled but no match found, may be intended so we warn user
            self.set_rulegen_preview_border_warn()
            self.set_rulegen_status(
                "Rule compiled, but no match found for %s" % idaapi.get_name(self.rulegen_current_function.start_ea)
            )

    def slot_limit_rulegen_features_to_search(self, text):
        """ """
        self.view_rulegen_features.filter_items_by_text(text)

    def slot_analyze(self):
        """run capa analysis and reload UI controls

        called when user selects plugin reload from menu
        """
        if self.view_tabs.currentIndex() == 0:
            self.analyze_program()
        elif self.view_tabs.currentIndex() == 1:
            self.analyze_function()

    def slot_reset(self):
        """reset UI elements

        e.g. checkboxes and IDA highlighting
        """
        if self.view_tabs.currentIndex() == 0:
            self.reset_program_analysis_views()
        elif self.view_tabs.currentIndex() == 1:
            self.reset_function_analysis_views()

    def slot_save(self):
        """ """
        if self.view_tabs.currentIndex() == 0:
            self.save_program_analysis()
        elif self.view_tabs.currentIndex() == 1:
            self.save_function_analysis()

    def save_program_analysis(self):
        """ """
        if not self.doc:
            idaapi.info("No program analysis to save.")
            return

        s = json.dumps(self.doc, sort_keys=True, cls=capa.render.CapaJsonObjectEncoder).encode("utf-8")

        path = idaapi.ask_file(True, "*.json", "Choose file to save capa program analysis JSON")
        if not path:
            return

        write_file(path, s)

    def save_function_analysis(self):
        """ """
        s = self.view_rulegen_preview.toPlainText().encode("utf-8")
        if not s:
            idaapi.info("No rule to save.")
            return

        path = idaapi.ask_file(True, "*.yml", "Choose file to save capa rule")
        if not path:
            return

        write_file(path, s)

    def slot_checkbox_limit_by_changed(self, state):
        """slot activated if checkbox clicked

        if checked, configure function filter if screen location is located in function, otherwise clear filter

        @param state: checked state
        """
        if state == QtCore.Qt.Checked:
            self.limit_results_to_function(idaapi.get_func(idaapi.get_screen_ea()))
        else:
            self.range_model_proxy.reset_address_range_filter()

        self.view_tree.reset_ui()

    def limit_results_to_function(self, f):
        """add filter to limit results to current function

        adds new address range filter to include function bounds, allowing basic blocks matched within a function
        to be included in the results

        @param f: (IDA func_t)
        """
        if f:
            self.range_model_proxy.add_address_range_filter(f.start_ea, f.end_ea)
        else:
            # if function not exists don't display any results (assume address never -1)
            self.range_model_proxy.add_address_range_filter(-1, -1)

    def slot_limit_results_to_search(self, text):
        """limit tree view results to search matches

        reset view after filter to maintain level 1 expansion
        """
        self.search_model_proxy.set_query(text)
        self.view_tree.reset_ui(should_sort=False)

    def ask_user_directory(self):
        """create Qt dialog to ask user for a directory"""
        return str(
            QtWidgets.QFileDialog.getExistingDirectory(
                self.parent, "Please select a capa rules directory", settings.user["rule_path"]
            )
        )

    def slot_change_rule_scope(self):
        """ """
        scope = idaapi.ask_str(settings.user.get("rulegen_scope", "function"), 0, "Enter default rule scope")
        if scope:
            settings.user["rulegen_scope"] = scope
            idaapi.info("Run analysis again for your changes to take effect.")

    def slot_change_rule_author(self):
        """ """
        author = idaapi.ask_str(settings.user.get("rulegen_author", ""), 0, "Enter default rule author")
        if author:
            settings.user["rulegen_author"] = author
            idaapi.info("Run analysis again for your changes to take effect.")

    def slot_change_rules_dir(self):
        """allow user to change rules directory

        user selection stored in settings for future runs
        """
        path = self.ask_user_directory()
        if path:
            settings.user["rule_path"] = path
            idaapi.info("Run analysis again for your changes to take effect.")

    def set_view_status_label(self, text):
        """update status label control

        @param text: updated text
        """
        self.view_status_label.setText(text)

    def disable_controls(self):
        """disable form controls"""
        self.view_reset_button.setEnabled(False)
        # self.view_tabs.setTabEnabled(0, False)
        # self.view_tabs.setTabEnabled(1, False)

    def enable_controls(self):
        """enable form controls"""
        self.view_reset_button.setEnabled(True)
        # self.view_tabs.setTabEnabled(0, True)
        # self.view_tabs.setTabEnabled(1, True)
