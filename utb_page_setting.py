#!/usr/bin/env python3
"""Under the Bar - Settings Page

This file provides the settings page for application, currently mainly provides routines to fetch API data.
"""

import sys
import json
import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QHBoxLayout,
    QVBoxLayout,
    QGridLayout,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QWidget,
)
from PySide6.QtGui import QPalette, QColor
from PySide6.QtGui import QIcon, QPixmap, QPainter
from PySide6.QtCore import Slot, Signal, QObject, QThreadPool, QRunnable

from dotenv import load_dotenv, set_key

import hevy_api
import strava_api
import utb_prs

STRAVA_SETTINGS_KEY = "strava-activity-type-filters"

#
# This view provides means to adjust settings and/or interact with the Hevy API
#
class Setting(QWidget):

	def __init__(self, color):
		super(Setting, self).__init__()
		home_folder = str(Path.home())
		utb_folder = home_folder + "/.underthebar"
		self.script_folder = os.path.split(os.path.abspath(__file__))[0]
		self.utb_folder = utb_folder

		session_data = {}
		if os.path.exists(utb_folder+"/session.json"):
			with open(utb_folder+"/session.json", 'r') as file:
				session_data = json.load(file)
		else:
			return 403
		user_folder = utb_folder + "/user_" + session_data["user-id"]
		if not os.path.exists(user_folder):
			os.makedirs(user_folder)
			os.makedirs(user_folder+"/workouts")
			os.makedirs(user_folder+"/routines")

		self.user_folder = utb_folder + "/user_" + session_data["user-id"]

		workoutcount_data = None
		if os.path.exists(user_folder+"/workout_count.json"):
			with open(user_folder+"/workout_count.json", 'r') as file:
				workoutcount_data = json.load(file)
		else:
			workoutcount_data = {"data": {"workout_count": 0}}

		# Load saved activity type filter settings (default: all enabled)
		all_types = [at.type for at in strava_api.ALL_ACTIVITY_TYPES]
		saved_filters = session_data.get(STRAVA_SETTINGS_KEY, all_types)

		# Outer layout holds the scroll area
		pagelayout = QVBoxLayout()
		scroll = QScrollArea()
		scroll.setWidgetResizable(True)
		scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		pagelayout.addWidget(scroll)
		self.setLayout(pagelayout)

		# Inner widget that lives inside the scroll area
		inner = QWidget()
		scroll.setWidget(inner)
		innerlayout = QHBoxLayout()
		innerlayout.addStretch()
		detailslayout = QVBoxLayout()
		detailslayout.setAlignment(Qt.AlignTop)
		innerlayout.addLayout(detailslayout)
		innerlayout.addStretch()
		inner.setLayout(innerlayout)

		# ── API data section ──────────────────────────────────────────────
		detailsgrid = QGridLayout()

		self.apiCallable = ["account","body_measurements","user_preferences","user_subscription","workout_count",]
		self.apiCallable_dict = {}
		self.apiCallable_button = []
		self.apiCallable_stateLabel = []
		for btnID in range(len(self.apiCallable)):
			self.apiCallable_dict[self.apiCallable[btnID]] = btnID

			thelabel = QLabel(self.apiCallable[btnID])
			thelabel.setFixedWidth(200)
			detailsgrid.addWidget(thelabel, btnID,0)

			btn = QPushButton()
			btn.setIcon(self.loadIcon(self.script_folder+"/icons/cloud-arrow-down-solid.svg"))
			btn.setIconSize(QSize(24,24))
			btn.clicked.connect(lambda *args, x=btnID: self.update_button_pushed(x))
			self.apiCallable_button.append(btn)
			detailsgrid.addWidget(btn,btnID,1)

			stateLabel = QLabel()
			stateLabel.setFixedWidth(200)
			self.apiCallable_stateLabel.append(stateLabel)
			detailsgrid.addWidget(stateLabel,btnID,2)

		detailslayout.addLayout(detailsgrid)

		# ── Workout sync section ──────────────────────────────────────────
		detailslayout.addWidget(QLabel("\nWorkout Synchronisation"))
		localWorkoutCount = len(os.listdir(user_folder+"/workouts"))
		self.localworkoutsLabel = QLabel("Local Workouts: "+str(localWorkoutCount))
		detailslayout.addWidget(self.localworkoutsLabel)
		self.remoteworkoutsLabel = QLabel("Remote Workouts: "+str(workoutcount_data["data"]["workout_count"]))
		detailslayout.addWidget(self.remoteworkoutsLabel)

		workoutsyncgrid = QGridLayout()

		workouts_batch_label = QLabel("workouts_batch")
		workouts_batch_label.setFixedWidth(200)
		workoutsyncgrid.addWidget(workouts_batch_label, 0,0)
		self.workoutsyncbtn = QPushButton()
		self.workoutsyncbtn.setIcon(self.loadIcon(self.script_folder+"/icons/cloud-arrow-down-solid.svg"))
		self.workoutsyncbtn.setIconSize(QSize(24,24))
		self.workoutsyncbtn.clicked.connect(lambda *args, x="workouts_batch": self.batch_button_pushed(x))
		workoutsyncgrid.addWidget(self.workoutsyncbtn,0,1)
		self.workoutsyncstateLabel = QLabel("Use for bulk downloads")
		self.workoutsyncstateLabel.setFixedWidth(200)
		workoutsyncgrid.addWidget(self.workoutsyncstateLabel,0,2)

		workouts_sync_batch_label = QLabel("workouts_sync_batch")
		workouts_sync_batch_label.setFixedWidth(200)
		workoutsyncgrid.addWidget(workouts_sync_batch_label, 1,0)
		self.workoutsyncbatchbtn = QPushButton()
		self.workoutsyncbatchbtn.setIcon(self.loadIcon(self.script_folder+"/icons/cloud-arrow-down-solid.svg"))
		self.workoutsyncbatchbtn.setIconSize(QSize(24,24))
		self.workoutsyncbatchbtn.clicked.connect(lambda *args, x="workouts_sync_batch": self.batch_button_pushed(x))
		workoutsyncgrid.addWidget(self.workoutsyncbatchbtn,1,1)
		self.workoutsyncbatchstateLabel = QLabel("Use for latest updates")
		self.workoutsyncbatchstateLabel.setFixedWidth(200)
		workoutsyncgrid.addWidget(self.workoutsyncbatchstateLabel,1,2)

		detailslayout.addLayout(workoutsyncgrid)

		# ── Strava section ────────────────────────────────────────────────
		detailslayout.addWidget(QLabel("\nStrava Import"))

		stravagrid = QGridLayout()

		strava_import_label = QLabel("strava import")
		strava_import_label.setFixedWidth(200)
		stravagrid.addWidget(strava_import_label, 0,0)
		self.stravaimportbtn = QPushButton()
		self.stravaimportbtn.setIcon(self.loadIcon(self.script_folder+"/icons/cloud-arrow-down-solid.svg"))
		self.stravaimportbtn.setIconSize(QSize(24,24))
		self.stravaimportbtn.clicked.connect(self.strava_import_clicked)
		stravagrid.addWidget(self.stravaimportbtn,0,1)
		self.stravaimportstateLabel = QLabel("Import most recent run/walk/hike/ride")
		self.stravaimportstateLabel.setFixedWidth(200)
		stravagrid.addWidget(self.stravaimportstateLabel,0,2)

		detailslayout.addLayout(stravagrid)

		if not strava_api._is_production():
			dev_warning = QLabel("⚠  DEV MODE — all imported activities will be set to private")
			dev_warning.setStyleSheet(
				"background-color: #f5a623; color: #000; font-weight: bold;"
				" padding: 4px 8px; border-radius: 4px;"
			)
			detailslayout.addWidget(dev_warning)

		# Activity type filter checkboxes
		filter_label = QLabel("Activity type filters:")
		detailslayout.addWidget(filter_label)
		filter_layout = QHBoxLayout()
		self.strava_type_checkboxes = {}
		for at in strava_api.ALL_ACTIVITY_TYPES:
			cb = QCheckBox(at.title)
			cb.setChecked(at.type in saved_filters)
			cb.stateChanged.connect(self.save_strava_type_filters)
			self.strava_type_checkboxes[at.type] = cb
			filter_layout.addWidget(cb)
		filter_layout.addStretch()
		detailslayout.addLayout(filter_layout)

		# Strava API credentials
		self.env_path = os.path.join(self.script_folder, ".env")
		load_dotenv(self.env_path, override=True)
		detailslayout.addWidget(QLabel("\nStrava API Credentials"))
		stravacredsgrid = QGridLayout()

		strava_id_label = QLabel("Client ID")
		strava_id_label.setFixedWidth(200)
		stravacredsgrid.addWidget(strava_id_label, 0, 0)
		self.stravaClientIdField = QLineEdit()
		self.stravaClientIdField.setText(os.environ.get("STRAVA_CLIENT_ID", ""))
		self.stravaClientIdField.setPlaceholderText("Strava Client ID")
		self.stravaClientIdField.setEchoMode(QLineEdit.Password)
		stravacredsgrid.addWidget(self.stravaClientIdField, 0, 1)

		strava_secret_label = QLabel("Client Secret")
		strava_secret_label.setFixedWidth(200)
		stravacredsgrid.addWidget(strava_secret_label, 1, 0)
		self.stravaClientSecretField = QLineEdit()
		self.stravaClientSecretField.setText(os.environ.get("STRAVA_CLIENT_SECRET", ""))
		self.stravaClientSecretField.setPlaceholderText("Strava Client Secret")
		self.stravaClientSecretField.setEchoMode(QLineEdit.Password)
		stravacredsgrid.addWidget(self.stravaClientSecretField, 1, 1)

		strava_save_btn = QPushButton("Save")
		strava_save_btn.clicked.connect(self.save_strava_credentials)
		stravacredsgrid.addWidget(strava_save_btn, 2, 0)

		self.stravaTestBtn = QPushButton("Test Connection")
		self.stravaTestBtn.clicked.connect(self.test_strava_connection)
		stravacredsgrid.addWidget(self.stravaTestBtn, 2, 1)

		self.stravaCredsStatusLabel = QLabel()
		stravacredsgrid.addWidget(self.stravaCredsStatusLabel, 3, 0, 1, 2)

		detailslayout.addLayout(stravacredsgrid)

		# ── Logout ────────────────────────────────────────────────────────
		detailslayout.addWidget(QLabel("\n"))
		self.log_out_button = QPushButton("Logout and Quit")
		self.log_out_button.setFixedWidth(200)
		self.log_out_button.clicked.connect(self.log_out_quit)
		detailslayout.addWidget(self.log_out_button)

		self.pool = QThreadPool()
		self.pool.setMaxThreadCount(5)

	def log_out_quit(self):
		print("Quitting...")
		hevy_api.logout()
		sys.exit()

	def get_enabled_strava_types(self):
		"""Return list of enabled activity type strings from checkboxes."""
		return [type_key for type_key, cb in self.strava_type_checkboxes.items() if cb.isChecked()]

	def save_strava_type_filters(self):
		"""Persist the current checkbox state to session.json."""
		enabled = self.get_enabled_strava_types()
		session_path = self.utb_folder + "/session.json"
		if os.path.exists(session_path):
			with open(session_path, 'r') as f:
				session_data = json.load(f)
			session_data[STRAVA_SETTINGS_KEY] = enabled
			with open(session_path, 'w') as f:
				json.dump(session_data, f)

	def save_strava_credentials(self):
		cl_id = self.stravaClientIdField.text().strip()
		cl_secret = self.stravaClientSecretField.text().strip()
		set_key(self.env_path, "STRAVA_CLIENT_ID", cl_id)
		set_key(self.env_path, "STRAVA_CLIENT_SECRET", cl_secret)
		os.environ["STRAVA_CLIENT_ID"] = cl_id
		os.environ["STRAVA_CLIENT_SECRET"] = cl_secret
		self.stravaCredsStatusLabel.setText("Saved")

	def test_strava_connection(self):
		self.stravaTestBtn.setEnabled(False)
		self.stravaCredsStatusLabel.setText("Testing...")
		cl_id = self.stravaClientIdField.text().strip()
		cl_secret = self.stravaClientSecretField.text().strip()
		worker = StravaTestWorker(cl_id, cl_secret)
		worker.emitter.done.connect(self.on_strava_test_done)
		self.pool.start(worker)

	@Slot(bool, str)
	def on_strava_test_done(self, success, message):
		self.stravaTestBtn.setEnabled(True)
		self.stravaCredsStatusLabel.setText(message)

	def strava_import_clicked(self):
		"""Start fetching recent activities then show the picker dialog."""
		self.stravaimportbtn.setIcon(self.loadIcon(self.script_folder+"/icons/spinner-solid.svg"))
		self.stravaimportbtn.setIconSize(QSize(24,24))
		self.stravaimportbtn.setEnabled(False)
		self.stravaimportstateLabel.setText("fetching activities...")
		enabled_types = self.get_enabled_strava_types()
		worker = StravaFetchWorker(enabled_types)
		worker.emitter.done.connect(self.on_fetch_activities_done)
		self.pool.start(worker)

	@Slot(int, list)
	def on_fetch_activities_done(self, status_code, activities):
		self.stravaimportbtn.setIcon(self.loadIcon(self.script_folder+"/icons/cloud-arrow-down-solid.svg"))
		self.stravaimportbtn.setIconSize(QSize(24,24))
		self.stravaimportbtn.setEnabled(True)

		if status_code != 200:
			if status_code == 404:
				self.stravaimportstateLabel.setText("API details not found")
			else:
				self.stravaimportstateLabel.setText("failed (code {})".format(status_code))
			return

		if not activities:
			self.stravaimportstateLabel.setText("No matching activities found")
			return

		# Show activity picker dialog
		dialog = StravaActivityPickerDialog(activities, self)
		if dialog.exec() == QDialog.Accepted:
			selected = dialog.selected_activity()
			if selected:
				self.stravaimportstateLabel.setText("importing...")
				self.stravaimportbtn.setEnabled(False)
				self.stravaimportbtn.setIcon(self.loadIcon(self.script_folder+"/icons/spinner-solid.svg"))
				self.stravaimportbtn.setIconSize(QSize(24,24))
				enabled_types = self.get_enabled_strava_types()
				worker = StravaImportWorker(selected["id"], enabled_types)
				worker.emitter.done.connect(self.on_import_done)
				self.pool.start(worker)
		else:
			self.stravaimportstateLabel.setText("cancelled")

	@Slot(int)
	def on_import_done(self, status_code):
		self.stravaimportbtn.setIcon(self.loadIcon(self.script_folder+"/icons/cloud-arrow-down-solid.svg"))
		self.stravaimportbtn.setIconSize(QSize(24,24))
		self.stravaimportbtn.setEnabled(True)
		if status_code == 200:
			self.stravaimportstateLabel.setText("completed")
		elif status_code == 404:
			self.stravaimportstateLabel.setText("API details not found")
		else:
			self.stravaimportstateLabel.setText("failed (code {})".format(status_code))

	def update_button_pushed(self, button_id):

		self.apiCallable_button[button_id].setIcon(self.loadIcon(self.script_folder+"/icons/spinner-solid.svg"))
		self.apiCallable_button[button_id].setIconSize(QSize(24,24))
		self.apiCallable_button[button_id].setEnabled(False)
		self.apiCallable_stateLabel[button_id].setText("updating...")

		worker = MyWorker(self.apiCallable[button_id],button_id)
		worker.emitter.done.connect(self.on_worker_done)
		self.pool.start(worker)

	def batch_button_pushed(self, name):
		print("start batch download",name)
		if name == "workouts_batch":
			self.workoutsyncbtn.setIcon(self.loadIcon(self.script_folder+"/icons/spinner-solid.svg"))
			self.workoutsyncbtn.setIconSize(QSize(24,24))
			self.workoutsyncbtn.setEnabled(False)
			self.workoutsyncbatchbtn.setEnabled(False)
			self.workoutsyncstateLabel.setText("updating...")
		elif name == "workouts_sync_batch":
			self.workoutsyncbatchbtn.setIcon(self.loadIcon(self.script_folder+"/icons/spinner-solid.svg"))
			self.workoutsyncbatchbtn.setIconSize(QSize(24,24))
			self.workoutsyncbatchbtn.setEnabled(False)
			self.workoutsyncbtn.setEnabled(False)
			self.workoutsyncbatchstateLabel.setText("updating...")
		worker = MyBatchWorker(name,0)
		worker.emitter.done.connect(self.on_batch_worker_done)
		self.pool.start(worker)

	@Slot(str,int,int)
	def on_worker_done(self, worker, orig_id, status):
		print("task completed:", worker)

		self.apiCallable_button[orig_id].setIcon(self.loadIcon(self.script_folder+"/icons/cloud-arrow-down-solid.svg"))
		self.apiCallable_button[orig_id].setIconSize(QSize(24,24))
		self.apiCallable_button[orig_id].setEnabled(True)

		if status == 200:
			self.apiCallable_stateLabel[orig_id].setText("updated")

			if worker == "workout_count":
				if os.path.exists(self.user_folder+"/workout_count.json"):
					with open(self.user_folder+"/workout_count.json", 'r') as file:
						workoutcount_data = json.load(file)
						self.remoteworkoutsLabel.setText("Remote Workouts: "+str(workoutcount_data["data"]["workout_count"]))
		elif status == 304:
			self.apiCallable_stateLabel[orig_id].setText("no update")


	@Slot(str,int,int)
	def on_batch_worker_done(self, worker, return_code, has_more):
		print("task completed:", worker, return_code, has_more)

		localWorkoutCount = len(os.listdir(self.user_folder+"/workouts"))
		self.localworkoutsLabel.setText("Local Workouts: "+str(localWorkoutCount))

		if has_more:
			if worker == "workouts_batch":
				self.workoutsyncstateLabel.setText("updating... more")
			elif worker == "workouts_sync_batch":
				self.workoutsyncbatchstateLabel.setText("updating... more")

		else:
			if worker == "workouts_batch":
				self.workoutsyncstateLabel.setText("updating PRs")
				utb_prs.do_the_thing()
				self.workoutsyncbtn.setIcon(self.loadIcon(self.script_folder+"/icons/cloud-arrow-down-solid.svg"))
				self.workoutsyncbtn.setIconSize(QSize(24,24))
				self.workoutsyncbtn.setEnabled(True)
				self.workoutsyncbatchbtn.setEnabled(True)
				self.workoutsyncstateLabel.setText("updated")
			elif worker == "workouts_sync_batch":
				self.workoutsyncbatchstateLabel.setText("updating PRs")
				utb_prs.do_the_thing()
				self.workoutsyncbatchbtn.setIcon(self.loadIcon(self.script_folder+"/icons/cloud-arrow-down-solid.svg"))
				self.workoutsyncbatchbtn.setIconSize(QSize(24,24))
				self.workoutsyncbtn.setEnabled(True)
				self.workoutsyncbatchbtn.setEnabled(True)
				self.workoutsyncbatchstateLabel.setText("updated")


	def loadIcon(self, path):
		img = QPixmap(path)
		qp = QPainter(img)
		qp.setCompositionMode(QPainter.CompositionMode_SourceIn)
		qp.fillRect( img.rect(), QColor(self.palette().color(QPalette.Text)) )
		qp.end()
		ic = QIcon(img)
		return ic


class StravaActivityPickerDialog(QDialog):
	"""Dialog that shows a list of recent Strava activities for the user to choose from."""

	def __init__(self, activities, parent=None):
		super().__init__(parent)
		self.setWindowTitle("Select Strava Activity to Import")
		self.activities = activities
		self._selected = None

		layout = QVBoxLayout()
		layout.addWidget(QLabel("Choose an activity to import:"))

		self.list_widget = QListWidget()
		for act in activities:
			date_str = act["start_date"].strftime("%Y-%m-%d %H:%M") if act["start_date"] else "?"
			dist_km = act["distance"] / 1000.0 if act["distance"] else 0
			duration_min = act["moving_time"] // 60 if act["moving_time"] else 0
			label = "{} — {} ({:.2f} km, {} min)".format(
				date_str, act["name"], dist_km, duration_min
			)
			item = QListWidgetItem(label)
			self.list_widget.addItem(item)
		self.list_widget.setCurrentRow(0)
		layout.addWidget(self.list_widget)

		buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
		buttons.accepted.connect(self.accept)
		buttons.rejected.connect(self.reject)
		layout.addWidget(buttons)

		self.setLayout(layout)
		self.resize(600, 300)

	def selected_activity(self):
		row = self.list_widget.currentRow()
		if row >= 0:
			return self.activities[row]
		return None


class MyEmitter(QObject):
	done = Signal(str,int,int)

class StravaTestEmitter(QObject):
	done = Signal(bool, str)

class StravaFetchEmitter(QObject):
	done = Signal(int, list)

class StravaImportEmitter(QObject):
	done = Signal(int)

class StravaTestWorker(QRunnable):

	def __init__(self, cl_id, cl_secret):
		super(StravaTestWorker, self).__init__()
		self.cl_id = cl_id
		self.cl_secret = cl_secret
		self.emitter = StravaTestEmitter()

	@Slot()
	def run(self):
		try:
			import requests
			if not self.cl_id or not self.cl_secret:
				self.emitter.done.emit(False, "Missing credentials")
				return
			resp = requests.post("https://www.strava.com/oauth/token", data={
				"client_id": self.cl_id,
				"client_secret": self.cl_secret,
				"grant_type": "client_credentials",
			})
			if resp.status_code == 401:
				self.emitter.done.emit(False, "Invalid client ID or secret")
			else:
				self.emitter.done.emit(True, "Credentials OK")
		except Exception as e:
			self.emitter.done.emit(False, "Error: " + str(e))


class StravaFetchWorker(QRunnable):
	"""Fetches the list of recent matching activities from Strava."""

	def __init__(self, enabled_types):
		super().__init__()
		self.enabled_types = enabled_types
		self.emitter = StravaFetchEmitter()

	@Slot()
	def run(self):
		try:
			status, activities = strava_api.get_recent_activities(self.enabled_types)
			self.emitter.done.emit(status, activities)
		except Exception as e:
			print("StravaFetchWorker exception:", e)
			self.emitter.done.emit(0, [])


class StravaImportWorker(QRunnable):
	"""Imports a specific Strava activity into Hevy."""

	def __init__(self, activity_id, enabled_types):
		super().__init__()
		self.activity_id = activity_id
		self.enabled_types = enabled_types
		self.emitter = StravaImportEmitter()

	@Slot()
	def run(self):
		try:
			status = strava_api.import_activity(self.activity_id, self.enabled_types)
			self.emitter.done.emit(status)
		except Exception as e:
			print("StravaImportWorker exception:", e)
			self.emitter.done.emit(0)


class MyWorker(QRunnable):

	def __init__(self, name, orig_id):
		super(MyWorker, self).__init__()
		self.name = name
		self.origId = orig_id
		self.emitter = MyEmitter()

	def run(self):
		status = hevy_api.update_generic(self.name)
		self.emitter.done.emit(str(self.name),self.origId,status)


class MyBatchWorker(QRunnable):

	def __init__(self, name, startIndex):
		super(MyBatchWorker, self).__init__()
		self.name = name
		self.startIndex = startIndex
		self.emitter = MyEmitter()

	@Slot()
	def run(self):
		keepGoing = True
		while keepGoing:
			status = (200, False)
			try:
				if self.name == "workouts_batch":
					status = hevy_api.batch_download()
				elif self.name == "workouts_sync_batch":
					status = hevy_api.workouts_sync_batch()
			except:
				print("exception")
				self.emitter.done.emit(str(self.name),0,False)
				break
			self.emitter.done.emit(str(self.name),status[0],status[1])
			keepGoing = status[1]

if __name__ == "__main__":
	app = QApplication(sys.argv)

	app.setStyle("Fusion")

	# Now use a palette to switch to dark colors:
	palette = QPalette()
	palette.setColor(QPalette.Window, QColor(53, 53, 53))
	palette.setColor(QPalette.WindowText, Qt.white)
	palette.setColor(QPalette.Base, QColor(25, 25, 25))
	palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
	palette.setColor(QPalette.ToolTipBase, Qt.black)
	palette.setColor(QPalette.ToolTipText, Qt.white)
	palette.setColor(QPalette.Text, Qt.white)
	palette.setColor(QPalette.Button, QColor(53, 53, 53))
	palette.setColor(QPalette.ButtonText, Qt.white)
	palette.setColor(QPalette.BrightText, Qt.red)
	palette.setColor(QPalette.Link, QColor(42, 130, 218))
	palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
	palette.setColor(QPalette.HighlightedText, Qt.black)
	app.setPalette(palette)



	window = Setting("red")
	window.resize(1200,800)
	window.show()

	app.exec_()
