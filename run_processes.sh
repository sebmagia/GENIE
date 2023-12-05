#!/bin/bash

# Number of processes to run in parallel
N=2  # Adjust this number according to your requirements

# Function to check the number of running processes
check_processes() {
    local running=$(jobs -p | wc -l)
    echo "$running"
}

# Loop to continuously spawn processes
for i in {0..60}; do
    echo "Starting process_continuous_days.py with argument $i"
    python process_continuous_days.py "$i" &
    ((count++))
    
    echo "Total processes running: $(check_processes)"

    # Check if the number of running processes is less than N
    while [ $(check_processes) -ge $N ]; do
        #echo "Reached maximum processes ($N), waiting..."
        sleep 1  # Sleep for a second before checking again
    done
done

# Wait for all remaining processes to finish
wait
