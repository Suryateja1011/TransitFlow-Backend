from flask import Flask, request, jsonify
from flask_cors import CORS
from ultralytics import YOLO
import cv2
import numpy as np
import math
import copy
import mysql.connector
from datetime import datetime, timedelta
import uuid
import os

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ---------------------------------
# YOLO MODEL
# ---------------------------------
model = YOLO("yolov8l.pt")

# ---------------------------------
# MYSQL CONNECTION
# ---------------------------------
db = mysql.connector.connect(
    host=os.environ["DB_HOST"],
    port=int(os.getenv("DB_PORT", 3306)),
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    database=os.getenv("DB_NAME", "smart_bus_system"),
    connection_timeout=10
)

cursor = db.cursor(dictionary=True, buffered=True)

# ---------------------------------
# CONSTRAINTS
# ---------------------------------
MAX_TRIPS_PER_DRIVER = 5
REST_MINUTES = 10

BUS_CAPACITY = 30
MIN_FREQUENCY_SEC = 10.0
MAX_BUSES_PER_ROUTE = 50
PENALTY_PER_BUS = 8.0

# ---------------------------------
# BRANCH SPLITS
# ---------------------------------
BRANCH_SPLITS = {
    "B3": 2,
    "B6": 3
}

# ---------------------------------
# ROUTE FETCHING
# ---------------------------------
def fetch_routes():

    cursor.execute("SELECT * FROM routes")
    routes_db = cursor.fetchall()

    # TEMPORARY STATIC PATH MAPPING
    # Later you can move this to DB
    route_paths = {
        "R001": ["B1", "B2", "B3", "B4", "B5"],
        "R002": ["B1", "B2", "B3", "B6", "B7"],
        "R003": ["B1", "B2", "B3", "B6", "B8", "B9"],
        "R004": ["B1", "B2", "B3", "B6", "B10"]
    }

    routes = []

    for r in routes_db:

        routes.append({
            "id": r["route_id"],
            "name": f'{r["start_stop"]} -> {r["end_stop"]}',
            "path": route_paths.get(r["route_id"], [])
        })

    return routes

# ---------------------------------
# DRIVER FETCH
# ---------------------------------
def get_available_driver():

    cursor.execute("""
    SELECT * FROM drivers
    WHERE availability = TRUE
    AND on_leave = FALSE
    AND total_trips_today < %s
""", (MAX_TRIPS_PER_DRIVER,))

    drivers = cursor.fetchall()

    for d in drivers:

        if d["last_trip_end"] is None:
            return d

        rest_completed = (
            datetime.now() - d["last_trip_end"]
        ) >= timedelta(minutes=REST_MINUTES)

        if rest_completed:
            return d

    return None

# ---------------------------------
# CONDUCTOR FETCH
# ---------------------------------
def get_available_conductor():

    cursor.execute("""
        SELECT * FROM conductors
        WHERE cond_availability = TRUE
        AND on_leave = FALSE
    """)

    return cursor.fetchone()
# ---------------------------------
# BUS FETCH
# ---------------------------------
def get_available_bus():

    cursor.execute("""
        SELECT * FROM buses
        WHERE service_status = 'Active'
    """)

    buses = cursor.fetchall()

    for b in buses:

        cursor.execute("""
            SELECT * FROM trips
            WHERE bus_id = %s
            AND trip_completed = FALSE
        """, (b["bus_id"],))

        active_trip = cursor.fetchone()

        if not active_trip:
            return b

    return None

# ---------------------------------
# CREATE TRIP
# ---------------------------------
def create_trip(route_id):

    driver = get_available_driver()
    conductor = get_available_conductor()
    bus = get_available_bus()

    if not driver or not conductor or not bus:
        return None

    trip_id = str(uuid.uuid4())[:8]

    start_time = datetime.now()
    end_time = start_time + timedelta(hours=2)

    cursor.execute("""
        INSERT INTO trips (
            trip_id,
            bus_id,
            driver_id,
            conductor_id,
            route_id,
            trip_start_time,
            trip_end_time,
            trip_completed
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        trip_id,
        bus["bus_id"],
        driver["driver_id"],
        conductor["conductor_id"],
        route_id,
        start_time,
        end_time,
        False
    ))

    # Mirror into trip_history for permanent record
    cursor.execute("""
        INSERT INTO trip_history (
            trip_id,
            bus_id,
            driver_id,
            conductor_id,
            route_id,
            trip_start_time,
            trip_end_time,
            trip_completed
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        trip_id,
        bus["bus_id"],
        driver["driver_id"],
        conductor["conductor_id"],
        route_id,
        start_time,
        end_time,
        False
    ))

    # UPDATE DRIVER
    cursor.execute("""
        UPDATE drivers
        SET
        availability = FALSE,
        total_trips_today = total_trips_today + 1
        WHERE driver_id = %s
    """, (driver["driver_id"],))

    # UPDATE CONDUCTOR
    cursor.execute("""
        UPDATE conductors
        SET
        cond_availability = FALSE
        WHERE conductor_id = %s
    """, (conductor["conductor_id"],))

    # UPDATE BUS
    cursor.execute("""
        UPDATE buses
        SET service_status = 'On Trip'
        WHERE bus_id = %s
    """, (bus["bus_id"],))

    db.commit()

    return True
# ---------------------------------
# FETCH ACTIVE TRIPS
# ---------------------------------
def fetch_active_trips():

    cursor.execute("""
        SELECT
            trip_id,
            bus_id,
            driver_id,
            conductor_id,
            route_id,
            trip_start_time,
            trip_completed
        FROM trips
        WHERE trip_completed = FALSE
        ORDER BY trip_start_time DESC
    """)

    trips = cursor.fetchall()

    formatted = []

    for t in trips:

        formatted.append({
            "trip_id": t["trip_id"],
            "bus_id": t["bus_id"],
            "driver_id": t["driver_id"],
            "conductor_id": t["conductor_id"],
            "route_id": t["route_id"],
            "trip_start_time":
                t["trip_start_time"].strftime("%H:%M:%S")
                if t["trip_start_time"] else None,
            "trip_completed": bool(t["trip_completed"])
        })

    return formatted
# ---------------------------------
# COMPLETE FINISHED TRIPS
# ---------------------------------
def complete_finished_trips():

    now = datetime.now()

    cursor.execute("""
        SELECT * FROM trips
        WHERE trip_completed = FALSE
        AND trip_end_time <= %s
    """, (now,))

    trips = cursor.fetchall()

    for t in trips:

        cursor.execute("""
            UPDATE trips
            SET trip_completed = TRUE
            WHERE trip_id = %s
        """, (t["trip_id"],))

        # Sync status to permanent history
        cursor.execute("""
            UPDATE trip_history
            SET trip_completed = TRUE, trip_end_time = %s
            WHERE trip_id = %s
        """, (now, t["trip_id"]))

        cursor.execute("""
            UPDATE drivers
            SET
            availability = TRUE,
            last_trip_end = %s
            WHERE driver_id = %s
        """, (now, t["driver_id"]))

        cursor.execute("""
    UPDATE conductors
    SET cond_availability = TRUE
    WHERE conductor_id = %s
""", (t["conductor_id"],))
        cursor.execute("""
            UPDATE buses
            SET service_status = 'Active'
            WHERE bus_id = %s
        """, (t["bus_id"],))

    db.commit()

# ---------------------------------
# YOLO PERSON COUNT
# ---------------------------------
def count_people_from_file(file):

    file_bytes = np.frombuffer(file.read(), np.uint8)

    img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)

    if img is None:
        return 0

    results = model(img)

    return sum(
        1
        for r in results
        for b in r.boxes
        if int(b.cls[0]) == 0
    )

# ---------------------------------
# WAIT CALCULATION
# ---------------------------------
def avg_wait_for_route(cycle_sec, buses_allocated):

    if buses_allocated <= 0:
        return float('inf')

    raw_freq = cycle_sec / buses_allocated \
        if cycle_sec > 0 else float('inf')

    freq = max(raw_freq, MIN_FREQUENCY_SEC)

    return freq / 2.0

# ---------------------------------
# OBJECTIVE FUNCTION
# ---------------------------------
def total_objective(route_infos, cycle_sec):

    total_wait = 0.0
    total_buses = 0

    for r in route_infos:

        ppl = r["total_people"]
        buses = r["buses_allocated"]

        if ppl <= 0:
            continue

        aw = avg_wait_for_route(cycle_sec, buses)

        total_wait += ppl * aw
        total_buses += buses

    return total_wait + PENALTY_PER_BUS * total_buses

# ---------------------------------
# MAIN API
# ---------------------------------
@app.route('/count_people', methods=['POST', 'OPTIONS'])

def count_people():

    if request.method == 'OPTIONS':
        return jsonify({"message": "preflight OK"}), 200

    # COMPLETE FINISHED TRIPS
    complete_finished_trips()
    # ---------------------------------
    # FETCH REAL ACTIVE BUS COUNT
    # ---------------------------------
    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM buses
        WHERE service_status = 'Active'
    """)

    db_available_buses = cursor.fetchone()["total"]

    # INPUTS
    try:
        total_buses = int(request.form.get("total_buses", 0))
    except:
        total_buses = 0
            # Use minimum of frontend input and actual DB buses
    total_buses = min(total_buses, db_available_buses)

    try:
        total_cycles = int(request.form.get("total_cycles", 0))
    except:
        total_cycles = 0

    if total_buses < 0:
        return jsonify({
            "error": "total_buses must be non-negative"
        }), 400

    # ---------------------------------
    # PEOPLE COUNTING
    # ---------------------------------
    slots = [f"B{i}" for i in range(1, 11)]

    slot_counts = {}

    for s in slots:

        if s in request.files:

            f = request.files[s]

            if f and f.filename:

                f.stream.seek(0)

                slot_counts[s] = int(
                    count_people_from_file(f)
                )

            else:
                slot_counts[s] = 0

        else:
            slot_counts[s] = 0

    # ---------------------------------
    # ROUTE AGGREGATION
    # ---------------------------------
    ROUTES = fetch_routes()

    route_data = []

    for route in ROUTES:

        total_people = 0.0

        for stop in route["path"]:

            p = slot_counts.get(stop, 0)

            if stop in BRANCH_SPLITS \
                    and BRANCH_SPLITS[stop] > 0:

                total_people += p / BRANCH_SPLITS[stop]

            else:
                total_people += p

        route_data.append({
            "route_id": route["id"],
            "route_name": route["name"],
            "total_people": float(round(total_people, 2))
        })

    # ---------------------------------
    # PROBABILITIES
    # ---------------------------------
    P_total = sum(r["total_people"] for r in route_data)

    for r in route_data:

        if P_total <= 0:
            r["probability"] = 0.0

        else:
            r["probability"] = round(
                (r["total_people"] / P_total) * 100.0,
                2
            )

    # ---------------------------------
    # VALIDATE MINIMUM FLEET
    # ---------------------------------
    num_routes = len(route_data)

    if total_buses < num_routes:

        return jsonify({
            "error":
            "Not enough buses to assign minimum 1 per route.",
            "required_minimum": num_routes,
            "provided": total_buses
        }), 400

    # ---------------------------------
    # MIN/MAX CALCULATIONS
    # ---------------------------------
    min_required = {}
    max_useful = {}

    for r in route_data:

        rid = r["route_id"]
        ppl = r["total_people"]

        if ppl <= 0:
            need = 1

        else:
            need = math.ceil(ppl / BUS_CAPACITY)
            need = max(1, need)

        need = min(need, MAX_BUSES_PER_ROUTE)

        min_required[rid] = need

        if total_cycles > 0:

            useful = int(
                math.floor(
                    total_cycles / MIN_FREQUENCY_SEC
                )
            )

            useful = max(1, useful)

        else:
            useful = MAX_BUSES_PER_ROUTE

        useful = min(useful, MAX_BUSES_PER_ROUTE)

        max_useful[rid] = useful

    # ---------------------------------
    # INITIAL ALLOCATION
    # ---------------------------------
    alloc = {
        r["route_id"]:
        min_required[r["route_id"]]
        for r in route_data
    }

    # ---------------------------------
    # ENFORCE FLEET CAP (largest-remainder method)
    # If individual ceil() values sum above total_buses, scale down.
    # ---------------------------------
    allocated_sum = sum(alloc.values())

    if allocated_sum > total_buses:
        # Compute ideal fractional shares from probability weights
        rids = [r["route_id"] for r in route_data]
        probs = [r["probability"] for r in route_data]
        total_prob = sum(probs) or 1.0

        # Compute exact fractional allocations
        exact = {
            rid: (probs[i] / total_prob) * total_buses
            for i, rid in enumerate(rids)
        }

        # Floor each, track remainders
        floored = {rid: int(math.floor(v)) for rid, v in exact.items()}
        remainders = {rid: exact[rid] - floored[rid] for rid in rids}

        # Ensure every route gets at least 1
        for rid in rids:
            floored[rid] = max(1, floored[rid])

        # Distribute any leftover slots to routes with biggest remainders
        given = sum(floored.values())
        leftover = total_buses - given

        if leftover > 0:
            sorted_rids = sorted(rids, key=lambda r: remainders[r], reverse=True)
            for rid in sorted_rids[:leftover]:
                floored[rid] += 1
        elif leftover < 0:
            # Over-allocated due to the max(1,...) — remove from smallest remainder first
            sorted_rids = sorted(rids, key=lambda r: remainders[r])
            for rid in sorted_rids:
                if leftover >= 0:
                    break
                if floored[rid] > 1:
                    floored[rid] -= 1
                    leftover += 1

        alloc = floored

    # ---------------------------------
    # ROUTE INFOS
    # ---------------------------------
    route_infos = []

    for r in route_data:

        route_infos.append({
            "route_id": r["route_id"],
            "route_name": r["route_name"],
            "total_people": r["total_people"],
            "probability": r["probability"],
            "buses_allocated":
            alloc[r["route_id"]]
        })

    # ---------------------------------
    # MARGINAL GAIN
    # ---------------------------------
    def marginal_gain_add(route_infos_local, idx):

        before = total_objective(
            route_infos_local,
            total_cycles if total_cycles > 0 else 1
        )

        cand = copy.deepcopy(route_infos_local)

        cand[idx]["buses_allocated"] += 1

        after = total_objective(
            cand,
            total_cycles if total_cycles > 0 else 1
        )

        return before - after

    # ---------------------------------
    # EXTRA ALLOCATION
    # ---------------------------------
    extra_slots = sum(
        max(
            0,
            max_useful[rid] - alloc[rid]
        )
        for rid in alloc
    )

    remaining_fleet = total_buses - sum(alloc.values())

    additions_allowed = min(
        remaining_fleet,
        extra_slots
    )

    for _ in range(additions_allowed):

        best_gain = 0.0
        best_idx = None

        for idx, rinfo in enumerate(route_infos):

            rid = rinfo["route_id"]

            if rinfo["buses_allocated"] >= max_useful[rid]:
                continue

            if rinfo["buses_allocated"] >= MAX_BUSES_PER_ROUTE:
                continue

            g = marginal_gain_add(route_infos, idx)

            if g > best_gain:
                best_gain = g
                best_idx = idx

        if best_idx is None or best_gain <= 1e-6:
            break

        route_infos[best_idx]["buses_allocated"] += 1

    # ---------------------------------
    # FINAL COUNTS
    # ---------------------------------
    final_used = sum(
        r["buses_allocated"]
        for r in route_infos
    )

    saved_buses = max(
        0,
        total_buses - final_used
    )

    # ---------------------------------
    # CREATE TRIPS
    # ---------------------------------
    for r in route_infos:

        buses_needed = r["buses_allocated"]

        for _ in range(buses_needed):

            create_trip(r["route_id"])

    # FETCH REAL ACTIVE TRIPS
    created_trips = fetch_active_trips()
    # ---------------------------------
    # RESPONSE
    # ---------------------------------
    response_routes = []

    for r in route_infos:

        buses = int(r["buses_allocated"])
        ppl = r["total_people"]

        if buses > 0:

            raw_freq = (
                total_cycles / buses
            ) if total_cycles > 0 else float('inf')

            freq_val = round(
                max(raw_freq, MIN_FREQUENCY_SEC),
                2
            )

        else:
            freq_val = None

        response_routes.append({
            "route_id": r["route_id"],
            "route_name": r["route_name"],
            "total_people": int(round(ppl)),
            "probability": round(r["probability"], 2),
            "buses_allocated": buses,
            "frequency_minutes": freq_val
        })

    slot_counts_int = {
        k: int(v)
        for k, v in slot_counts.items()
    }

    return jsonify({
        "slot_counts": slot_counts_int,
        "routes": response_routes,
        "saved_buses": int(saved_buses),
        "created_trips": created_trips
    }), 200
# ---------------------------------
# COMPLETE TRIP MANUALLY
# ---------------------------------
@app.route('/complete_trip', methods=['POST'])
def complete_trip():

    data = request.json

    bus_id = data.get("bus_id")

    if not bus_id:
        return jsonify({
            "error": "bus_id required"
        }), 400

    # Find active trip
    cursor.execute("""
        SELECT *
        FROM trips
        WHERE bus_id = %s
        AND trip_completed = FALSE
    """, (bus_id,))

    trip = cursor.fetchone()

    if not trip:
        return jsonify({
            "error": "No active trip found"
        }), 404

    now = datetime.now()

    # Mark trip completed
    cursor.execute("""
        UPDATE trips
        SET
        trip_completed = TRUE,
        trip_end_time = %s
        WHERE trip_id = %s
    """, (
        now,
        trip["trip_id"]
    ))

    # Sync status to permanent history
    cursor.execute("""
        UPDATE trip_history
        SET trip_completed = TRUE, trip_end_time = %s
        WHERE trip_id = %s
    """, (now, trip["trip_id"]))

    # Driver enters rest mode
    cursor.execute("""
        UPDATE drivers
        SET
        availability = TRUE,
        last_trip_end = %s
        WHERE driver_id = %s
    """, (
        now,
        trip["driver_id"]
    ))

    # Bus becomes active
    cursor.execute("""
        UPDATE buses
        SET service_status = 'Active'
        WHERE bus_id = %s
    """, (bus_id,))

    # Conductor becomes available
    cursor.execute("""
        UPDATE conductors
        SET cond_availability = TRUE
        WHERE conductor_id = %s
    """, (trip["conductor_id"],))

    db.commit()

    return jsonify({
        "message": "Trip completed successfully",
        "trip_id": trip["trip_id"]
    }), 200
# ---------------------------------
# MARK STAFF ON LEAVE
# ---------------------------------
@app.route('/mark_leave', methods=['POST'])
def mark_leave():

    data = request.json

    staff_id = data.get("staff_id")

    if not staff_id:
        return jsonify({
            "error": "staff_id required"
        }), 400

    # DRIVER
    if staff_id.startswith("D"):

        cursor.execute("""
            UPDATE drivers
            SET
            availability = FALSE,
            on_leave = TRUE
            WHERE driver_id = %s
        """, (staff_id,))

        if cursor.rowcount == 0:
            return jsonify({
                "error": "Driver not found"
            }), 404

        db.commit()

        return jsonify({
            "message": f"Driver {staff_id} marked on leave"
        }), 200

    # CONDUCTOR
        # CONDUCTOR
    elif staff_id.startswith("C"):

        cursor.execute("""
            UPDATE conductors
            SET
            cond_availability = FALSE,
            on_leave = TRUE
            WHERE conductor_id = %s
        """, (staff_id,))

        if cursor.rowcount == 0:
            return jsonify({
                "error": "Conductor not found"
            }), 404

        db.commit()

        return jsonify({
            "message": f"Conductor {staff_id} marked on leave"
        }), 200

    else:
        return jsonify({
            "error": "Invalid ID format"
        }), 400

# ---------------------------------
# NEW REST ENDPOINTS FOR TRANSITFLOW V3.0
# ---------------------------------

@app.route('/trip_history', methods=['GET'])
def get_trip_history():
    try:
        cursor.execute("""
            SELECT
                th.trip_id,
                th.bus_id,
                th.driver_id,
                th.conductor_id,
                th.route_id,
                th.trip_start_time,
                th.trip_end_time,
                th.trip_completed
            FROM trip_history th
            ORDER BY th.trip_start_time DESC
        """)
        history = cursor.fetchall()
        formatted = []
        for h in history:
            formatted.append({
                "trip_id": h["trip_id"],
                "bus_id": h["bus_id"],
                "driver_id": h["driver_id"],
                "conductor_id": h["conductor_id"],
                "route_id": h["route_id"],
                "trip_start_time": h["trip_start_time"].strftime("%Y-%m-%d %H:%M:%S") if h["trip_start_time"] else None,
                "trip_end_time": h["trip_end_time"].strftime("%Y-%m-%d %H:%M:%S") if h["trip_end_time"] else None,
                "trip_completed": bool(h["trip_completed"])
            })
        return jsonify(formatted), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/reset_system', methods=['POST'])
def reset_system():
    try:
        cursor.execute("DELETE FROM trips")
        cursor.execute("UPDATE buses SET service_status = 'Active'")
        cursor.execute("""
            UPDATE drivers
            SET availability = TRUE,
                total_trips_today = 0,
                last_trip_end = NULL,
                on_leave = FALSE
        """)
        cursor.execute("""
            UPDATE conductors
            SET cond_availability = TRUE,
                assigned_bus = NULL,
                on_leave = FALSE
        """)
        db.commit()
        return jsonify({
            "success": True,
            "message": "System reset successfully"
        }), 200
    except Exception as e:
        db.rollback()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/drivers', methods=['GET', 'POST'])
def handle_drivers():
    if request.method == 'GET':
        try:
            cursor.execute("SELECT * FROM drivers ORDER BY driver_id")
            drivers = cursor.fetchall()
            # Format datetime columns and calculate real-time resting state
            now = datetime.now()
            for d in drivers:
                d["is_resting"] = False
                d["rest_remaining_minutes"] = 0
                if d.get("last_trip_end"):
                    elapsed_time = now - d["last_trip_end"]
                    rest_duration = timedelta(minutes=REST_MINUTES)
                    if elapsed_time < rest_duration:
                        # Only show as resting if they are marked available (i.e. not actively on a new trip)
                        if d.get("availability"):
                            d["is_resting"] = True
                            remaining = rest_duration - elapsed_time
                            d["rest_remaining_minutes"] = math.ceil(remaining.total_seconds() / 60.0)
                    
                    d["last_trip_end"] = d["last_trip_end"].strftime("%Y-%m-%d %H:%M:%S")
            return jsonify(drivers), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    elif request.method == 'POST':
        data = request.json
        driver_id = data.get("driver_id")
        name = data.get("name")
        phone = data.get("phone")
        
        if not driver_id or not name or not phone:
            return jsonify({"error": "Missing required fields"}), 400
            
        try:
            cursor.execute("""
                INSERT INTO drivers (driver_id, name, phone, total_trips_today, availability, on_leave)
                VALUES (%s, %s, %s, 0, TRUE, FALSE)
            """, (driver_id, name, phone))
            db.commit()
            return jsonify({"success": True, "message": "Driver added successfully"}), 201
        except Exception as e:
            db.rollback()
            return jsonify({"error": str(e)}), 500

@app.route('/conductors', methods=['GET', 'POST'])
def handle_conductors():
    if request.method == 'GET':
        try:
            cursor.execute("SELECT * FROM conductors ORDER BY conductor_id")
            conductors = cursor.fetchall()
            return jsonify(conductors), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    elif request.method == 'POST':
        data = request.json
        conductor_id = data.get("conductor_id")
        name = data.get("name")
        phone = data.get("phone")
        
        if not conductor_id or not name or not phone:
            return jsonify({"error": "Missing required fields"}), 400
            
        try:
            cursor.execute("""
                INSERT INTO conductors (conductor_id, name, phone, cond_availability, on_leave)
                VALUES (%s, %s, %s, TRUE, FALSE)
            """, (conductor_id, name, phone))
            db.commit()
            return jsonify({"success": True, "message": "Conductor added successfully"}), 201
        except Exception as e:
            db.rollback()
            return jsonify({"error": str(e)}), 500

@app.route('/buses', methods=['GET', 'POST'])
def handle_buses():
    if request.method == 'GET':
        try:
            cursor.execute("""
                SELECT b.*, t.route_id AS assigned_route
                FROM buses b
                LEFT JOIN trips t ON b.bus_id = t.bus_id AND t.trip_completed = FALSE
                ORDER BY b.bus_id
            """)
            buses = cursor.fetchall()
            return jsonify(buses), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    elif request.method == 'POST':
        data = request.json
        bus_id = data.get("bus_id")
        bus_number = data.get("bus_number")
        capacity = data.get("capacity")
        
        if not bus_id or not bus_number or capacity is None:
            return jsonify({"error": "Missing required fields"}), 400
            
        try:
            cursor.execute("""
                INSERT INTO buses (bus_id, bus_number, capacity, service_status, fuel_type, depot_id)
                VALUES (%s, %s, %s, 'Active', 'Electric', 'DEP01')
            """, (bus_id, bus_number, int(capacity)))
            db.commit()
            return jsonify({"success": True, "message": "Bus added successfully"}), 201
        except Exception as e:
            db.rollback()
            return jsonify({"error": str(e)}), 500

# ---------------------------------
# RUN SERVER
# ---------------------------------
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
