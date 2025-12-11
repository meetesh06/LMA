import csv
import os
import argparse
import subprocess
import json
from typing import List, Tuple, Dict, Optional

if os.geteuid() != 0:
    print("This script must be run as root!")
    sys.exit(1)

LMA_VERSION = "1.0"

LMA_DATA_PATH = "/usr/lib/lma/lmaData"
DATABASE_FILE = "/usr/lib/lma/lmaAllocations.csv"
TOTAL_START = None
TOTAL_END = None
ALLOCATION_OPTIONS = None
BASH_SCRIPT_PATH = "/usr/lib/lma/lma-aHook.sh"
DEALLOCATION_SCRIPT_PATH = "/usr/lib/lma/lma-dHook.sh"

try:
    with open(LMA_DATA_PATH, "r") as f:
        LMA_DATA = json.load(f)
        if (LMA_DATA["version"] != LMA_VERSION):
            print(f"LMA config version mismatch")
            exit(1)
        TOTAL_START = LMA_DATA["cpu"]["isolated"][0]
        TOTAL_END = LMA_DATA["cpu"]["isolated"][1]
        ALLOCATION_OPTIONS = LMA_DATA["coreGroupSizes"]

except:
    print(f"LMA failed, lma-setup must be run before this command can be used")
    exit(1)


def initialize_database():
    """Ensures the CSV file exists with the correct header."""
    if not os.path.exists(DATABASE_FILE):
        print(f"Initializing database file: {DATABASE_FILE}")
        with open(DATABASE_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'start_core', 'end_core'])

def load_allocations() -> List[Tuple[int, int, int]]:
    """Loads existing core allocations from the CSV file."""
    initialize_database()
    allocations = []
    with open(DATABASE_FILE, 'r', newline='') as f:
        reader = csv.reader(f)
        next(reader) 
        for row in reader:
            try:
                allocations.append((int(row[0]), int(row[1]), int(row[2])))
            except ValueError as e:
                print(f"Skipping invalid row in CSV: {row} ({e})")
    return allocations

def save_allocation(user_id: int, start: int, end: int):
    """Appends a new core allocation to the CSV file."""
    with open(DATABASE_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([user_id, start, end])

def rewrite_allocations(allocations: List[Tuple[int, int, int]]):
    """Overwrites the database file with the current list of allocations."""
    with open(DATABASE_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'start_core', 'end_core'])
        for user_id, start, end in allocations:
            writer.writerow([user_id, start, end])

def find_first_fit(allocations: List[Tuple[int, int, int]], required_size: int) -> Optional[Tuple[int, int]]:
    """Finds the first available contiguous block of core IDs."""
    allocations.sort(key=lambda x: x[1])
    current_start = TOTAL_START
    for _, alloc_start, alloc_end in allocations:
        gap_start = current_start
        gap_end = alloc_start - 1
        if gap_end >= gap_start and (gap_end - gap_start + 1) >= required_size:
            new_end = gap_start + required_size - 1
            return (gap_start, new_end)
        current_start = alloc_end + 1
    
    if TOTAL_END >= current_start:
        remaining_size = TOTAL_END - current_start + 1
        if remaining_size >= required_size:
            new_end = current_start + required_size - 1
            return (current_start, new_end)
    return None

def display_map(allocations: List[Tuple[int, int, int]]):
    """Prints a simple representation of the core map."""
    print("\n" + "="*50)
    print(f"Isolated Cores: [{TOTAL_START} - {TOTAL_END}]")
    print("="*50)
    allocations.sort(key=lambda x: x[1])
    last_end = TOTAL_START - 1
    for user_id, start, end in allocations:
        if start > last_end + 1:
            print(f"  [{last_end + 1:3d} - {start - 1:3d}] -> FREE Cores ({start - 1 - last_end} units)")
        print(f"  [{start:3d} - {end:3d}] -> ALLOCATED (ID: {user_id}, Cores: {end - start + 1})")
        last_end = end
    if TOTAL_END > last_end:
        print(f"  [{last_end + 1:3d} - {TOTAL_END:3d}] -> FREE Cores ({TOTAL_END - last_end} units)")
    print("="*50 + "\n")

def get_user_selection(options: List[str]) -> str:
    """Presents a list of options (A, B, C...) and gets valid user input."""
    options_map: Dict[str, str] = {}
    print("\nChoose an action:")
    for i, option_text in enumerate(options):
        key = chr(ord('a') + i)
        options_map[key] = option_text
        print(f"  ({key}) {option_text}")
    while True:
        choice = input("Enter your choice: ").strip().lower()
        if choice in options_map:
            return choice
        else:
            print("Invalid choice. Please try again.")

# --- Hook Functions ---

def run_allocation_hook(user_id: int, start: int, end: int, size: int):
    """Executes the custom bash script for ALLOCATION."""
    try:
        command = [BASH_SCRIPT_PATH, str(user_id), str(start), str(end), str(size)]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        print("\n[Allocation Hook Output]")
        print(result.stdout.strip())
    except FileNotFoundError:
        print(f"\n[Hook Error] Allocation script not found at {BASH_SCRIPT_PATH}. Skipping hook.")
        exit(1)
    except subprocess.CalledProcessError as e:
        print(f"\n[Hook Error] Allocation script failed with return code {e.returncode}.")
        print(f"Stderr: {e.stderr.strip()}")
        exit(1)
    except Exception as e:
        print(f"\n[Hook Error] An unexpected error occurred: {e}")
        exit(1)

def run_deallocation_hook(user_id: int, start: int, end: int, size: int):
    """NEW: Executes the custom bash script for DEALLOCATION."""
    try:
        command = [DEALLOCATION_SCRIPT_PATH, str(user_id), str(start), str(end), str(size)]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        print("\n[Deallocation Hook Output]")
        print(result.stdout.strip())
        exit(1)
    except FileNotFoundError:
        print(f"\n[Hook Error] Deallocation script not found at {DEALLOCATION_SCRIPT_PATH}. Skipping hook.")
        exit(1)
    except subprocess.CalledProcessError as e:
        print(f"\n[Hook Error] Deallocation script failed with return code {e.returncode}.")
        print(f"Stderr: {e.stderr.strip()}")
        exit(1)
    except Exception as e:
        print(f"\n[Hook Error] An unexpected error occurred: {e}")
        exit(1)


def deallocate_space(user_id: int, allocations: List[Tuple[int, int, int]]):
    """Removes the user's core allocation from the list, rewrites the database, and runs the hook."""
    initial_count = len(allocations)
    
    # Identify the block being freed for logging and hook
    freed_block = next((a for a in allocations if a[0] == user_id), (0, 0, 0))
    
    # Calculate size for the hook
    start, end = freed_block[1], freed_block[2]
    size = end - start + 1
    
    # Keep only the allocations that do NOT belong to the current user_id
    new_allocations = [
        alloc for alloc in allocations if alloc[0] != user_id
    ]
    
    if len(new_allocations) < initial_count:        
        run_deallocation_hook(user_id, start, end, size)

        rewrite_allocations(new_allocations)
        print("\n" + "#"*50)
        print(f"🗑️  SUCCESSFULLY DEALLOCATED core IDs for User {user_id}.")
        print(f"   The core block is now free: **[{start} - {end}]**.")
        print("#"*50)

        return True
    else:
        print(f"❌ ERROR: User {user_id} was not found in the allocation list.")
        return False

def allocate_space(user_id: int, allocations: List[Tuple[int, int, int]]):
    """Prompts for size and attempts to find and allocate core space."""
    
    available_options_for_display = []
    for size in ALLOCATION_OPTIONS:
        if find_first_fit(allocations, size) is not None:
            available_options_for_display.append(size)

    if not available_options_for_display:
        print("ERROR: No contiguous core block is available for any of the standard sizes (16, 32, 64).")
        return

    print("\nChoose an allocation size (Cores):")
    exitOpt = 0
    for i, size in enumerate(available_options_for_display):
        key = chr(ord('a') + i)
        exitOpt = i
        print(f"  ({key}) {size} cores")

    exitChar = chr(ord('a') + exitOpt + 1)
    print(f"  ({exitChar}) Keep existing cores and Exit")

    selected_size = 0
    while True:
        choice = input("Enter your choice (e.g., 'a'): ").strip().lower()
        option_index = ord(choice) - ord('a')

        if exitChar == choice:
            print("Operation cancelled. Existing allocation preserved.")
            exit(0)
        
        if 0 <= option_index < len(available_options_for_display):
            selected_size = available_options_for_display[option_index]
            break
        else:
            print("Invalid choice. Please try again.")

    allocation_result = find_first_fit(allocations, selected_size)
    
    if allocation_result:
        start, end = allocation_result
        size = end - start + 1
        
        run_allocation_hook(user_id, start, end, size)

        save_allocation(user_id, start, end)
        print("\n" + "#"*50)
        print(f"✅ SUCCESSFULLY ALLOCATED {size} cores to User {user_id}")
        print(f"   Core ID Range: **[{start} - {end}]**")
        print("#"*50)

    else:
        print(f"❌ ERROR: Could not find a contiguous block of size {selected_size}.")

def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(description="Simple Core Allocation Script.")
    parser.add_argument("user_id", type=int, help="The ID of the user requesting core allocation (a number).")
    args = parser.parse_args()
    
    user_id = args.user_id
    print(f"--- LMA Core allocation for User ID: {user_id} ---")

    allocations = load_allocations()
    display_map(allocations)
    
    existing_allocation = next(
        ((id, start, end) for id, start, end in allocations if id == user_id), 
        None
    )
    
    if existing_allocation:
        _, start, end = existing_allocation
        size = end - start + 1
        print(f"⚠️ User {user_id} currently holds core block: **[{start} - {end}]** (Cores: {size})")
        
        choice_key = get_user_selection(["Deallocate existing cores", "Keep existing cores and Exit"])
        
        if choice_key == 'a': 
            if deallocate_space(user_id, allocations):
                final_allocations = load_allocations()
                display_map(final_allocations)
        else: 
            print("Operation cancelled. Existing allocation preserved.")
        
    else:
        allocate_space(user_id, allocations)
        
        final_allocations = load_allocations()
        if final_allocations != allocations:
             display_map(final_allocations)

if __name__ == "__main__":
    main()