"""Employee attendance API. MongoDB stores instants as naive UTC datetimes."""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=1500)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")

@app.on_event("startup")
def indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_day")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_period")

def epoch_dt(value: int) -> datetime:
    if type(value) is not int or not 100000000000 <= value <= 4102444800000:
        raise ValueError("must be an epoch millisecond integer in the supported range")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)

def dt_epoch(value):
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)

def trunc_dt(value):
    if value.tzinfo is None: value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)

def attendance_date(punch, emp):
    local = punch.astimezone(IST)
    d = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        d -= timedelta(days=1)
    return d.isoformat()

def shift_instant(day, clock, overnight=False):
    d = date.fromisoformat(day) + (timedelta(days=1) if overnight else timedelta())
    h, m = map(int, clock.split(":"))
    return datetime.combine(d, time(h,m), IST).astimezone(UTC)

def late_minutes(punch, shift_start, shift_end="18:30"):
    local = punch.astimezone(IST)
    overnight = shift_end <= shift_start
    if overnight and local.strftime("%H:%M") < shift_end:
        local -= timedelta(days=1)
    start = local.replace(hour=int(shift_start[:2]), minute=int(shift_start[3:]), second=0, microsecond=0)
    elapsed = int((local-start).total_seconds())
    return elapsed//60 if elapsed > 600 else 0

def work_hours(start, end):
    n = (Decimal(str((end-start).total_seconds())) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(n)


def overtime_minutes(end, shift_end, day, overnight):
    end = trunc_dt(end)
    edge = shift_instant(day, shift_end, overnight)
    edge = trunc_dt(edge)

    minutes = int((end - edge).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def encode_doc(doc):
    if not doc: return doc
    doc.pop("_id", None)
    for k in ("punch_in", "punch_out"):
        if k in doc: doc[k] = dt_epoch(doc[k])
    doc.setdefault("half_day", False); doc.setdefault("history", [])
    for entry in doc["history"]:
        entry["at"] = dt_epoch(entry.get("at"))
        for k, change in entry.get("changes", {}).items():
            if k in ("punch_in", "punch_out"):
                change["from"] = dt_epoch(change.get("from")); change["to"] = dt_epoch(change.get("to"))
    return doc

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date
    @model_validator(mode="after")
    def different_shift(self):
        if self.shift_start == self.shift_end: raise ValueError("shift times must differ")
        return self

class PunchInIn(StrictModel):
    emp_code: str
    punched_at: StrictInt | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_time(cls,v):
        if v is not None: epoch_dt(v)
        return v

class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: StrictInt | None = None
    @field_validator("punched_at")
    @classmethod
    def valid_time(cls,v):
        if v is not None: epoch_dt(v)
        return v

class RegularizeIn(StrictModel):
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_time(cls,v):
        if v is not None: epoch_dt(v)
        return v
    @model_validator(mode="after")
    def reject_null_times(self):
        if "punch_in" in self.model_fields_set and self.punch_in is None:
            raise ValueError("punch_in cannot be null")
        if "punch_out" in self.model_fields_set and self.punch_out is None:
            raise ValueError("punch_out cannot be null")
        return self

def page_params(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    return page, page_size

@app.get("/health")
def health():
    try: db.command("ping")
    except Exception: raise HTTPException(503, "database unavailable")
    return {"status":"ok"}

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc=body.model_dump(); doc["joined_on"]=body.joined_on.isoformat(); doc["created_at"]=datetime.now(UTC).replace(microsecond=0)
    
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = dt_epoch(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(department: str|None=None, page:int=Query(1,ge=1), page_size:int=Query(20,ge=1,le=100)):
    q={"department":department} if department is not None else {}
    total=db.employees.count_documents(q)
    items=list(db.employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size))
    for x in items: x["created_at"]=dt_epoch(x.get("created_at"))
    return {"items":items,"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=epoch_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    day=attendance_date(ts,emp)
    doc={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,"late_minutes":late_minutes(ts,emp["shift_start"],emp["shift_end"]),"overtime_minutes":0,"half_day":False,"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return encode_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = (
        epoch_dt(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts.replace(tzinfo=None)},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, "no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    # MongoDB stores naive UTC datetimes; convert to aware UTC.
    record_punch_in = record["punch_in"]
    if record_punch_in.tzinfo is None:
        record_punch_in = record_punch_in.replace(tzinfo=UTC)

    if (
        ts <= record_punch_in
        or ts - record_punch_in > timedelta(hours=24)
    ):
        raise HTTPException(422, "invalid punch-out time")

    overnight = emp["shift_end"] <= emp["shift_start"]
    hours = work_hours(record_punch_in, ts)

    update = {
        "punch_out": ts.replace(tzinfo=None),
        "work_hours": hours,
        "overtime_minutes": overtime_minutes(
            ts, emp["shift_end"], record["date"], overnight
        ),
        "half_day": hours < 4.50,
    }

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )

    if result is None:
        raise HTTPException(409, "already punched out")

    return encode_doc(result)

@app.get("/attendance")
def list_attendance(emp_code:str|None=None,date_from:date|None=None,date_to:date|None=None,status:Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]|None=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must be <= date_to")
    q={}
    if emp_code:q["emp_code"]=emp_code
    if date_from or date_to:
        q["date"]={}
        if date_from:q["date"]["$gte"]=date_from.isoformat()
        if date_to:q["date"]["$lte"]=date_to.isoformat()
    if status:q["status"]=status
    total=db.attendance_logs.count_documents(q)
    cur=db.attendance_logs.find(q).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items":[encode_doc(x) for x in cur],"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code:str,date:date,body:RegularizeIn):
    day_s=date.isoformat(); emp=db.employees.find_one({"emp_code":emp_code}); old=db.attendance_logs.find_one({"emp_code":emp_code,"date":day_s})
    if not emp or not old: raise HTTPException(404,"employee or attendance record not found")
    final_status=body.status or old["status"]
    if final_status in ("ABSENT","LEAVE") and ("punch_in" in body.model_fields_set or "punch_out" in body.model_fields_set): raise HTTPException(422,"absence and leave cannot have punch times")
    pin=epoch_dt(body.punch_in) if "punch_in" in body.model_fields_set and body.punch_in is not None else (None if final_status in ("ABSENT","LEAVE") else old.get("punch_in"))
    pout=epoch_dt(body.punch_out) if "punch_out" in body.model_fields_set and body.punch_out is not None else (None if final_status in ("ABSENT","LEAVE") else old.get("punch_out"))
    if pin is not None:pin = trunc_dt(pin)
    if pout is not None:pout = trunc_dt(pout)
    if old.get("punch_in") is not None:old["punch_in"] = trunc_dt(old["punch_in"])
    if old.get("punch_out") is not None:old["punch_out"] = trunc_dt(old["punch_out"])
    if final_status in PRESENCE and pin is None: raise HTTPException(422,"presence requires punch_in")
    if pin and attendance_date(pin,emp)!=day_s: raise HTTPException(422,"punch_in must match attendance date")
    if pout and (not pin or pout<=pin or pout-pin>timedelta(hours=24)): raise HTTPException(422,"invalid punch_out")
    late=late_minutes(pin,emp["shift_start"],emp["shift_end"]) if pin and final_status in PRESENCE else 0
    hours=work_hours(pin,pout) if pin and pout and final_status in PRESENCE else None
    overtime=overtime_minutes(pout,emp["shift_end"],day_s,emp["shift_end"]<=emp["shift_start"]) if hours is not None else 0
    half=hours is not None and hours<4.5
    new={"status":final_status,"punch_in":pin,"punch_out":pout,"work_hours":hours,"late_minutes":late,"overtime_minutes":overtime,"half_day":half}
    changes={}
    for k,v in new.items():
        before=old.get(k,False if k=="half_day" else 0 if k in ("late_minutes","overtime_minutes") else None)
        if before!=v: changes[k]={"from":before,"to":v}
    if not changes: raise HTTPException(422,"request changes nothing")
    hist={"at":datetime.now(UTC).replace(microsecond=0),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    guard={"history":old["history"]} if "history" in old else {"history":{"$exists":False}}
    result=db.attendance_logs.find_one_and_update({"_id":old["_id"],**guard}, {"$set":new,"$push":{"history":hist}},return_document=ReturnDocument.AFTER)
    if not result: raise HTTPException(409,"record changed concurrently")
    return encode_doc(result)

def month_dates(month):
    try: return date.fromisoformat(month+"-01"), (date.fromisoformat(month+"-01").replace(day=28)+timedelta(days=4)).replace(day=1)-timedelta(days=1)
    except ValueError: raise HTTPException(422,"month must be YYYY-MM")

def round_num(value, places=2):
    if value is None:return None
    return float(Decimal(str(value)).quantize(Decimal("1").scaleb(-places),rounding=ROUND_HALF_UP))

def month_match(month):
    first,last=month_dates(month)
    return {"date":{"$gte":first.isoformat(),"$lte":last.isoformat()}},first,last

def build_employee_monthly_pipeline(emp_code, month):
    q,_,_=month_match(month)
    pipeline=[{"$match":{"emp_code":emp_code,**q}},{"$set":{"_weekday":{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},"half_day":{"$ifNull":["$half_day",False]}}},{"$group":{"_id":None,"present":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$ne":["$_weekday",1]},{"$ne":["$_weekday",7]}]},{"$cond":["$half_day",0.5,1]},0]}},"leave":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"late":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},"late_mins":{"$sum":{"$ifNull":["$late_minutes",0]}},"ot":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}]
    return pipeline


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str=Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp:raise HTTPException(404,"employee not found")
    q,first,last=month_match(month); start=max(first,date.fromisoformat(emp["joined_on"]))
    working=sum(1 for i in range((last-start).days+1) if (start+timedelta(days=i)).weekday()<5) if start<=last else 0
    pipeline=build_employee_monthly_pipeline(emp_code,month)
    x=list(db.attendance_logs.aggregate(pipeline)); x=x[0] if x else {}
    present=round_num(x.get("present",0)); pct=round_num(present/working*100,4) if working else None
    return {"emp_code":emp_code,"month":month,"working_days":working,"present_days":present,"leave_days":x.get("leave",0),"late_count":x.get("late",0),"total_late_minutes":x.get("late_mins",0),"total_overtime_minutes":x.get("ot",0),"attendance_pct":pct}



def build_department_summary_pipeline(month, department):
    _, first, last = month_match(month)
    pipe = [
        {
            "$match": {
                "joined_on": {"$lte": last.isoformat()},
                **({"department": department} if department is not None else {}),
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", first.isoformat()]},
                                    {"$lte": ["$date", last.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "_weekday": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                            "half_day": {"$ifNull": ["$half_day", False]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "headcount": {"$literal": 1},
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$log.status", list(PRESENCE)]},
                                            {"$ne": ["$$log._weekday", 1]},
                                            {"$ne": ["$$log._weekday", 7]},
                                        ]
                                    },
                                    {"$cond": ["$$log.half_day", 0.5, 1]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
                "work_hours_values": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$log.status", list(PRESENCE)]},
                                        {"$ne": ["$$log.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "log",
                        "in": "$$log.work_hours",
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "all_work_hours": {"$push": "$work_hours_values"},
            }
        },
        {
            "$set": {
                "all_work_hours": {
                    "$reduce": {
                        "input": "$all_work_hours",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                }
            }
        },
        {
            "$set": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$all_work_hours"}, 0]},
                        {"$avg": "$all_work_hours"},
                        None,
                    ]
                }
            }
        },
        {"$unset": "all_work_hours"},
        {"$sort": {"_id": 1}},
    ]
    return pipe


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
):
    _, first, last = month_match(month)

    pipe = build_department_summary_pipeline(month, department)

    items = [
        {"department": x.pop("_id"), **x}
        for x in db.employees.aggregate(pipe, allowDiskUse=True)
    ]

    for item in items:
        item["present_days"] = round_num(item["present_days"])
        item["avg_work_hours"] = round_num(item["avg_work_hours"])

    return {"month": month, "items": items}

def build_late_leaderboard_pipeline(month, limit, department):
    q,_,_=month_match(month)
    pipe=[{"$match":q},{"$group":{"_id":"$emp_code","total":{"$sum":{"$ifNull":["$late_minutes",0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}},{"$match":{"total":{"$gt":0}}},{"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},{"$unwind":"$employee"}]
    if department is not None:pipe.append({"$match":{"employee.department":department}})
    pipe += [{"$setWindowFields":{"sortBy":{"total":-1},"output":{"rank":{"$rank":{}}}}},{"$match":{"rank":{"$lte":limit}}},{"$sort":{"total":-1,"_id":1}},{"$project":{"_id":0,"rank":1,"emp_code":"$_id","name":"$employee.name","department":"$employee.department","total_late_minutes":"$total","late_count":1}}]
    return pipe


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str=Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),limit:int=Query(10,ge=1,le=50),department:str|None=None):
    pipe=build_late_leaderboard_pipeline(month,limit,department)
    return {"month":month,"items":list(db.attendance_logs.aggregate(pipe,allowDiskUse=True))}

def build_department_trend_pipeline(department, from_day, to_day):
    round4=lambda expr:{"$divide":[{"$floor":{"$add":[{"$multiply":[expr,10000]},0.5]}},10000]}
    pipeline=[{"$match":{"date":{"$gte":from_day.isoformat(),"$lte":to_day.isoformat()}}},{"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"employee"}},{"$unwind":"$employee"},{"$match":{"employee.department":department}},{"$set":{"_day":{"$dateFromString":{"dateString":"$date","timezone":"Asia/Kolkata"}},"_half":{"$ifNull":["$half_day",False]}}},{"$group":{"_id":"$_day","present":{"$sum":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":["$_half",0.5,1]},0]}},"late":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}},
        # Seed every date from one department employee. This guarantees a fully empty
        # attendance range still has calendar rows; the range is generated in MongoDB.
        {"$unionWith":{"coll":"employees","pipeline":[{"$match":{"department":department}},{"$limit":1},{"$set":{"_start":{"$dateFromString":{"dateString":from_day.isoformat(),"timezone":"Asia/Kolkata"}},"_days":{"$range":[0,(to_day-from_day).days+1]}}},{"$unwind":"$_days"},{"$project":{"_id":{"$dateAdd":{"startDate":"$_start","unit":"day","amount":"$_days"}}}}]}},
        {"$group":{"_id":"$_id","present":{"$sum":{"$ifNull":["$present",0]}},"late":{"$sum":{"$ifNull":["$late",0]}}}},
        {"$densify":{"field":"_id","range":{"step":1,"unit":"day","bounds":[datetime.combine(from_day,time.min,IST).astimezone(UTC),datetime.combine(to_day+timedelta(days=1),time.min,IST).astimezone(UTC)]}}},{"$set":{"date":{"$dateToString":{"format":"%Y-%m-%d","date":"$_id","timezone":"Asia/Kolkata"}},"present":{"$ifNull":["$present",0]},"late":{"$ifNull":["$late",0]}}},{"$lookup":{"from":"employees","let":{"day":"$date"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$department",department]},{"$lte":["$joined_on","$$day"]}]}}},{"$count":"n"}],"as":"hc"}},{"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]},"is_working_day":{"$not":[{"$in":[{"$dayOfWeek":{"date":"$_id","timezone":"Asia/Kolkata"}},[1,7]]}]}}},{"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$divide":["$present","$headcount"]},None]}}},{"$setWindowFields":{"sortBy":{"_id":1},"output":{"_avg":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},{"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":"$present","late_count":"$late","attendance_rate":{"$cond":[{"$ne":["$attendance_rate",None]},round4("$attendance_rate"),None]},"moving_avg_7d":{"$cond":[{"$ne":["$_avg",None]},round4("$_avg"),None]}}},{"$sort":{"date":1}}]
    return pipeline


@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_day:date=Query(alias="from"),to_day:date=Query(alias="to")):
    if to_day<from_day or (to_day-from_day).days+1>92:raise HTTPException(422,"invalid trend range")
    if not db.employees.find_one({"department":department}):raise HTTPException(404,"department not found")
    pipeline=build_department_trend_pipeline(department,from_day,to_day)
    rows=list(db.attendance_logs.aggregate(pipeline,allowDiskUse=True))
    return {"department":department,"items":rows}

@app.get("/admin/explain/{endpoint}")
def explain(endpoint:Literal["attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"],emp_code:str|None=None,month:str|None=None,department:str|None=None,limit:int=Query(10,ge=1,le=50),date_from:date|None=None,date_to:date|None=None,status:Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]|None=None,from_day:date|None=Query(None,alias="from"),to_day:date|None=Query(None,alias="to"),page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if endpoint=="attendance_list":
        if date_from and date_to and date_from>date_to:raise HTTPException(422,"date_from must be <= date_to")
        q={}
        if emp_code:q["emp_code"]=emp_code
        if status:q["status"]=status
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=date_from.isoformat()
            if date_to:q["date"]["$lte"]=date_to.isoformat()
        ex=db.command("explain",{"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size},verbosity="executionStats"); collection="attendance_logs"
    else:
        if not month and endpoint!="department_trend":raise HTTPException(422,"month is required")
        if endpoint=="employee_monthly" and not emp_code:raise HTTPException(422,"emp_code is required")
        if endpoint=="department_trend" and (not department or not from_day or not to_day):raise HTTPException(422,"department, from and to are required")
        if month is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])",month):
            raise HTTPException(422,"month must be YYYY-MM")
        if endpoint=="employee_monthly":
            coll=db.attendance_logs
            pipeline=build_employee_monthly_pipeline(emp_code,month)
        elif endpoint=="late_leaderboard":
            coll=db.attendance_logs
            pipeline=build_late_leaderboard_pipeline(month,limit,department)
        elif endpoint=="department_trend":
            if to_day<from_day or (to_day-from_day).days+1>92:raise HTTPException(422,"invalid trend range")
            coll=db.attendance_logs
            pipeline=build_department_trend_pipeline(department,from_day,to_day)
        else:
            coll=db.employees
            pipeline=build_department_summary_pipeline(month,department)
        ex=db.command("explain",{"aggregate":coll.name,"pipeline":pipeline,"cursor":{}},verbosity="executionStats")
        collection=coll.name
    return {"endpoint":endpoint,"collection":collection,"explain":ex}
