#!/bin/bash

# Start the first process
python manage.py runserver 0.0.0.0:8000 &
status=$?
if [ $status -ne 0 ]; then
  echo "Failed to start Server: $status"
  exit $status
fi

# Start the second process
python manage.py runworker gateway &
status=$?
if [ $status -ne 0 ]; then
  echo "Failed to start Gateway: $status"
  exit $status
fi

# Start the second process
python manage.py runreserver &
status=$?
if [ $status -ne 0 ]; then
  echo "Failed to start Reserver: $status"
  exit $status
fi

# Naive check runs checks once a minute to see if either of the processes exited.
# This illustrates part of the heavy lifting you need to do if you want to run
# more than one service in a container. The container exits with an error
# if it detects that either of the processes has exited.
# Otherwise it loops forever, waking up every 60 seconds

while sleep 10; do
  ps aux |grep runserver |grep -q -v grep
  PROCESS_1_STATUS=$?
  ps aux |grep rungateway |grep -q -v grep
  PROCESS_2_STATUS=$?
  ps aux |grep runreserver |grep -q -v grep
  PROCESS_3_STATUS=$?
  # If the greps above find anything, they exit with 0 status
  # If they are not both 0, then something is wrong
  if [ $PROCESS_1_STATUS -ne 0 -o $PROCESS_2_STATUS -ne 0 -o $PROCESS_3_STATUS -ne 0 ]; then
    echo "One of the processes has already exited."
    exit 1
  fi
done