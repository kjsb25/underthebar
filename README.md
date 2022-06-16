# Under the Bar
All I want to do is drink beer and train like an animal.

## About
Under the Bar is a third-party client application for the Hevy workout tracking website and mobile applications.

This will only be useful if you have an existing Hevy account. See Hevyapp.com

It is not intended to be feature-complete. Currently it maintains a local copy of all of your workout data and provides some graphical analyis of that data.

## Prerequisites / Credits
Stuff I'm using to make this work for me:
- Python3
- matplotlib
- PySide2
- python_dateutil
- requests
- Font Awesome icons

## To run it
Execute the python file "underthebar.py"
- It should prompt you to log in to Hevy
- Probably will then display a blank profile page
- Go to settings page (bottom left gear button) and use "workouts_batch" to download your workouts_batch
- Use the other API buttons to download other data such as body measurements and personal records
- Go back to profile page and it should populate... yes?

Currently always starts with blank profile page, but just click on the profile page button to populate.

User data gets stored in ~/.underthebar

## Profile page
Top of the profile page displays basic profile info like your profile pic.

On the left is the Hevy feed (under the Hevy logo)
- Refresh button clears and reloads the two most recent workouts for you and the people you follow.
- "Plus" button adds additional workouts to the feed (i.e. more historical).
- The feed is also infinite scrolling, when you get to the end it will auto-add more workouts.
- Workouts can be "liked" by clicking the "thumbs up", but commenting is not currently supported.

Next is the calendar heat map which shows which days you've done a workout.
- Select a workout day for its details to be displayed below.
- Select a month header for a summary of that month to be displayed.

Lastly is just some stats:
- A tracking list of your body measurements
- A list of personal records for exercises that have been completed recently

## Analysis page
To get graphs go to the analysis page (the second, graph-looking button on the left)
- Select desired graph from the top list
- Select desired exercise/option from the second list
- Click "(re)generate" and graph should soon appear... yes?

Graphs are saved as images in the user data folder. To redraw when you have new data navigate to the graph again and select "(re)generate".

Sure dynamic and interactive graphs would be better... but this works for me for now.

