# How to Make Maki Start Automatically with Windows

Use Windows Task Scheduler to launch Maki whenever you log in.

---

## Step-by-Step Instructions

### 1. Open Task Scheduler

Press **Win + S**, search for **Task Scheduler**, and open it.

### 2. Create a New Task

In the right panel, click **"Create Basic Task…"**

### 3. Name and Description

- **Name:** `Maki Assistant`
- **Description:** `Start the Maki voice assistant on login`

Click **Next**.

### 4. Set the Trigger

- Select: **"When I log on"**

Click **Next**.

### 5. Set the Action

- Select: **"Start a program"**

Click **Next**.

### 6. Configure the Program

**Program/script:**
```
C:\Users\<you>\projectmaki\.venv\Scripts\pythonw.exe
```

> Use `pythonw.exe` (not `python.exe`) so no black console window appears.

**Add arguments (optional):**
```
main.py
```

**Start in:**
```
C:\Users\<you>\projectmaki
```

Click **Next**, then **Finish**.

---

## Test It

Right-click the new task in Task Scheduler and select **"Run"** to test it immediately without logging out.

---

## To Disable Auto-Start

Right-click the task → **Disable** (or **Delete** to remove it permanently).

---

## If Maki Doesn't Start at Login

1. Make sure the `.venv` was created in `C:\Users\<you>\projectmaki`
2. Open Task Scheduler, right-click the task → **Properties** → **General tab**
   - Check "Run only when user is logged on"
   - Uncheck "Run with highest privileges" (unless needed)
3. Check the **History** tab in Task Scheduler for error messages
4. Try running the task manually first to confirm it works

---

## Alternative: Startup Folder Method

If Task Scheduler feels complicated, use the Startup folder instead.

1. Press **Win + R**, type `shell:startup`, press Enter
2. Create a shortcut to this script in that folder

Create a file called `start_maki.bat` with:
```bat
@echo off
cd /d C:\Users\<you>\projectmaki
call .venv\Scripts\activate
start pythonw.exe main.py
```

Place `start_maki.bat` in the Startup folder. It will run on login.
