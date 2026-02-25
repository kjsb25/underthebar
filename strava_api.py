from pathlib import Path
import os
import json
import http.server
import socketserver
from urllib.parse import urlparse, parse_qs
import sys
from stravalib import Client
from datetime import datetime, timedelta
import requests, json
import webbrowser
import uuid
import random
import copy
from dotenv import load_dotenv

class Server(socketserver.TCPServer):

    # Avoid "address already used" error when frequently restarting the script
    allow_reuse_address = True


class Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        try:
            code_value = str(parse_qs(urlparse(self.path).query)["code"][0])
            self.send_response(200, "OK")
            self.end_headers()
            self.wfile.write("It worked! Code returned, you can close the browser window.".encode("utf-8"))

            print("Writing strava code to file")

            home_folder = str(Path.home())
            utb_folder = home_folder + "/.underthebar"
            session_data = {}
            if os.path.exists(utb_folder+"/session.json"):
                with open(utb_folder+"/session.json", 'r') as file:
                    session_data = json.load(file)
                    session_data["strava-token-code"] = code_value
                with open(utb_folder+"/session.json", 'w') as file:
                    json.dump(session_data,file)

        except:

            self.send_response(200, "OK")
            self.end_headers()
            self.wfile.write("Failed. Didn't find code.".encode("utf-8"))

	# note had to add this to allow server to work properly when run in window without console
    def log_message(self, format, *args):
        return


# Define activity object for easy adding and searching
class ActivityType:
	def __init__(self, type, title, id):
		self.type = type
		self.title = title
		self.id = id

	# matches if the activity type matches directly, or if it matches the root of the type
	def matches(self, activity_type):
		return (activity_type == self.type or
				str(activity_type) == f"root='{self.type}'")

# All activity types that can be pulled into Hevy
ALL_ACTIVITY_TYPES = [
	ActivityType("Run", "Running", "AC1BB830"),
	ActivityType("Ride", "Cycling", "D8F7F851"),
	ActivityType("Walk", "Walking", "33EDD7DB"),
	ActivityType("Hike", "Hiking", "1C34A172"),
]


def _get_client():
	"""Authenticate with Strava and return an authenticated client, or None on failure."""
	script_folder = os.path.split(os.path.abspath(__file__))[0]
	load_dotenv(os.path.join(script_folder, ".env"))
	cl_id = os.environ.get("STRAVA_CLIENT_ID")
	cl_secret = os.environ.get("STRAVA_CLIENT_SECRET")

	if not cl_id or not cl_secret:
		print("NEED TO ADD YOUR STRAVA API DETAILS TO .env !!!\n"*5)
		return None, 404

	home_folder = str(Path.home())
	utb_folder = home_folder + "/.underthebar"
	session_data = {}
	if os.path.exists(utb_folder+"/session.json"):
		with open(utb_folder+"/session.json", 'r') as file:
			session_data = json.load(file)

	get_token_url = "strava-token-refresh" not in session_data
	get_token_refresh = False
	get_token_access = not get_token_url

	client = Client()

	if get_token_url:
		url = client.authorization_url(client_id=cl_id, redirect_uri='http://localhost:8888/authorization', scope=['activity:read'])
		print(url)
		webbrowser.open(url, new=1, autoraise=True)
		print("Waiting for web browser response ....")
		with Server(("", 8888), Handler) as httpd:
			httpd.handle_request()

		if os.path.exists(utb_folder+"/session.json"):
			with open(utb_folder+"/session.json", 'r') as file:
				session_data = json.load(file)

		get_token_refresh = True

	if get_token_refresh:
		token_response = client.exchange_code_for_token(client_id=cl_id,
												  client_secret=cl_secret,
												  code=session_data["strava-token-code"])
		access_token = token_response['access_token']
		refresh_token = token_response['refresh_token']
		client = Client(access_token=access_token)
		session_data["strava-token-refresh"] = refresh_token
		with open(utb_folder+"/session.json", 'w') as file:
			json.dump(session_data, file)
		get_token_access = False

	if get_token_access:
		token_response = client.refresh_access_token(client_id=cl_id,
										  client_secret=cl_secret,
										  refresh_token=session_data["strava-token-refresh"])
		new_access_token = token_response['access_token']
		client = Client(access_token=new_access_token)

	return client, session_data


def _get_submittable_types(enabled_types, session_data):
	"""Return the list of ActivityType objects that are currently enabled."""
	submittable = [at for at in ALL_ACTIVITY_TYPES if at.type in enabled_types]

	# Note that this is a custom exercise that only exists for me in Hevy...
	if session_data.get("user-id") == "f21f5af1-a602-48f0-82fb-ed09bc984326":
		virtual_ride = ActivityType("VirtualRide", "Cycling (Virtual)", "89f3ed93-5418-4cc6-a114-0590f2977ae8")
		if "VirtualRide" in enabled_types:
			submittable.append(virtual_ride)

	return submittable


def get_recent_activities(enabled_types=None):
	"""
	Fetch up to 5 recent Strava activities matching the enabled types.
	Returns (status_code, list_of_activity_info_dicts) where each dict has:
	  id, name, type, type_title, start_date, distance, moving_time
	"""
	if enabled_types is None:
		enabled_types = [at.type for at in ALL_ACTIVITY_TYPES]

	print("starting strava_api > get_recent_activities()")
	client, session_data = _get_client()
	if client is None:
		return session_data, []  # session_data is the error code here

	athlete = client.get_athlete()
	print("Hello from Strava, {}".format(athlete.firstname))

	submittable_types = _get_submittable_types(enabled_types, session_data)

	# Fetch recent activities and filter to matching types, up to 5
	activities = client.get_activities(limit=20)
	matching = []
	for activity in activities:
		print(activity.type, activity.name, activity.start_date)
		for submittable_activity_type in submittable_types:
			if submittable_activity_type.matches(activity.type):
				matching.append({
					"id": activity.id,
					"name": activity.name,
					"type": submittable_activity_type.type,
					"type_title": submittable_activity_type.title,
					"start_date": activity.start_date,
					"distance": float(activity.distance) if activity.distance else 0,
					"moving_time": int(activity.moving_time) if activity.moving_time else 0,
				})
				break
		if len(matching) >= 5:
			break

	return 200, matching


def import_activity(activity_id, enabled_types=None):
	"""
	Import a specific Strava activity (by id) into Hevy.
	Returns status code (200 on success).
	"""
	if enabled_types is None:
		enabled_types = [at.type for at in ALL_ACTIVITY_TYPES]

	print("starting strava_api > import_activity()", activity_id)
	client, session_data = _get_client()
	if client is None:
		return session_data  # error code

	submittable_types = _get_submittable_types(enabled_types, session_data)

	# JSON of a basic workout, we'll adjust this
	run_template_tocopy = {
	  "workout": {
		"workout_id": "3413fa99-ace5-4209-b997-1ca3251f9fbc",
		"title": "Running (import)",
		"description": "(Import from Strava)",
		"exercises": [
		  {
			"title": "Running",
			"exercise_template_id": "AC1BB830",
			"rest_timer_seconds": 0,
			"notes": "",
			"volume_doubling_enabled": False,
			"sets": [
			  {
				"index": 0,
				"type": "normal",
				"distance_meters": 10030,
				"duration_seconds": 4101,
				"completed_at": "2025-08-23T00:53:43.532Z"
			  }
			]
		  }
		],
		"start_time": 1755906339,
		"end_time": 1755910466,
		"apple_watch": False,
		"wearos_watch": False,
		"is_private": True,
		"is_biometrics_public": True
	  },
	  "share_to_strava": False,
	  "strava_activity_local_time": "2025-8-23T9:15:39Z"
	}

	BASIC_HEADERS = {
		'x-api-key': 'with_great_power',
		'Content-Type': 'application/json',
		'accept-encoding':'gzip'
	}

	activity = client.get_activity(activity_id)

	matched_type = None
	for submittable_activity_type in submittable_types:
		if submittable_activity_type.matches(activity.type):
			matched_type = submittable_activity_type
			break

	if matched_type is None:
		print("Activity type not in submittable types")
		return 400

	run_template = copy.deepcopy(run_template_tocopy)
	print("Importing", activity.name, activity.start_date)

	run_template["workout"]["title"] = activity.name
	run_template["workout"]["exercises"][0]["title"] = matched_type.title
	run_template["workout"]["exercises"][0]["exercise_template_id"] = matched_type.id

	run_template["workout"]["start_time"] = int(activity.start_date.timestamp())
	run_template["workout"]["end_time"] = int(activity.start_date.timestamp() + activity.moving_time)
	run_template["strava_activity_local_time"] = activity.start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
	run_template["workout"]["exercises"][0]["sets"][0]["duration_seconds"] = int(activity.moving_time)
	run_template["workout"]["exercises"][0]["sets"][0]["distance_meters"] = int(activity.distance)
	run_template["workout"]["exercises"][0]["sets"][0]["completed_at"] = (activity.start_date + timedelta(seconds=activity.moving_time)).strftime('%Y-%m-%dT%H:%M:%SZ')

	if activity.description:
		run_template["workout"]["description"] = str(activity.description) + "\n\n" + run_template["workout"]["description"]
	if activity.device_name:
		run_template["workout"]["description"] = run_template["workout"]["description"] + "(" + str(activity.device_name) + ")"

	if activity.average_heartrate:
		run_template["workout"]["exercises"][0]["notes"] = "Heartrate Avg: " + str(activity.average_heartrate) + "bpm, Max: " + str(activity.max_heartrate) + "bpm."

		streams = client.get_activity_streams(activity.id, types=["time", "heartrate"])
		samples = []
		for datapoint in range(0, len(streams["time"].data)):
			samples.append({"timestamp_ms": int((activity.start_date.timestamp() + streams["time"].data[datapoint]) * 1000), "bpm": streams["heartrate"].data[datapoint]})

		run_template["workout"]["biometrics"] = {"total_calories": activity.calories, "heart_rate_samples": samples}

	if activity.average_watts:
		run_template["workout"]["exercises"][0]["notes"] += "\nPower Avg: " + str(activity.average_watts) + "W, Max: " + str(activity.max_watts) + "W."

	# Log in to Hevy and submit
	import hevy_api
	user_data = hevy_api.is_logged_in()
	if user_data[0] == False:
		print("403")
		return 403
	user_folder = user_data[1]
	auth_token = user_data[2]

	s = requests.Session()
	s.headers.update({'Authorization': "Bearer " + auth_token})
	headers = BASIC_HEADERS.copy()

	r = s.get("https://api.hevyapp.com/account", headers=headers)
	data = r.json()
	username = data["username"]
	print("Hello from Hevy,", username)

	rnd = random.Random()
	rnd.seed(run_template["workout"]["start_time"])
	local_id = uuid.UUID(int=rnd.getrandbits(128), version=4)
	run_template["workout"]["workout_id"] = str(local_id)

	workout_id = str(local_id)
	payload = json.dumps(run_template)
	r = s.post('https://api.hevyapp.com/v2/workout', data=payload, headers=headers)
	print(f"Hevy POST {r.status_code}")

	if r.status_code == 409:
		# Workout already exists — update it via PUT
		print(f"Workout {workout_id} already exists, updating via PUT")
		r = s.put(f'https://api.hevyapp.com/v2/workout/{workout_id}', data=payload, headers=headers)
		print(f"Hevy PUT {r.status_code}")

	if r.status_code not in (200, 201):
		print(f"Import failed with status {r.status_code}")
		return r.status_code

	print("success")
	return 200


def do_the_thing():
	"""Legacy entry point: fetch activities and import the first matching one automatically."""
	print("starting strava_api > do_the_thing()")
	enabled_types = [at.type for at in ALL_ACTIVITY_TYPES]
	status, activities = get_recent_activities(enabled_types)
	if status != 200:
		return status
	if not activities:
		print("No valid entries to import.")
		return 200
	first = activities[0]
	return import_activity(first["id"], enabled_types)


if __name__ == "__main__":
	do_the_thing()
