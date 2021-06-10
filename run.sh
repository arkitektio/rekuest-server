#!/bin/bash
echo "=> Waiting for DB to be online"
python manage.py wait_for_database -s 2

echo "=> Performing database migrations..."
python manage.py migrate

echo "=> Ensuring Superusers..."
python manage.py ensureadmin

echo "=> Collecting Static.."
python manage.py collectstatic --noinput

# Start the first process
echo "=> Starting Server in Background"
python manage.py runserver 0.0.0.0:8090 &
status=$?
if [ $status -ne 0 ]; then
  echo "Failed to start Server: $status"
  exit $status
fi

# Start the second process

echo "=> Starting Gateway in Background"
python manage.py runworker gateway &
status=$?
if [ $status -ne 0 ]; then
  echo "Failed to start Gateway: $status"
  exit $status
fi

# Start the second process
echo "=> Starting Router in Background"
python manage.py runreserver &
status=$?
if [ $status -ne 0 ]; then
  echo "Failed to start Router: $status"
  exit $status
fi

# Naive check runs checks once a minute to see if either of the processes exited.
# This illustrates part of the heavy lifting you need to do if you want to run
# more than one service in a container. The container exits with an error
# if it detects that either of the processes has exited.
# Otherwise it loops forever, waking up every 60 seconds

while sleep 10; do
  ps aux |grep "runserver" |grep -q -v grep
  PROCESS_1_STATUS=$?
  ps aux |grep "runworker gateway" |grep -q -v grep
  PROCESS_2_STATUS=$?
  ps aux |grep "runreserver" |grep -q -v grep
  PROCESS_3_STATUS=$?
  # If the greps above find anything, they exit with 0 status
  # If they are not both 0, then something is wrong
  if [ $PROCESS_1_STATUS -ne 0 -o $PROCESS_2_STATUS -ne 0 -o $PROCESS_3_STATUS -ne 0 ]; then
    echo "One of the processes has already exited."
    exit 1
  fi
done