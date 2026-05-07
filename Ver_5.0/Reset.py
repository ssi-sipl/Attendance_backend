from pyfingerprint.pyfingerprint import PyFingerprint
import sys

try:
    # Initialize sensor
    f = PyFingerprint('/dev/serial0', 57600, 0xFFFFFFFF, 0x00000000)

    if not f.verifyPassword():
        raise ValueError("Fingerprint sensor not found or password incorrect")

    print("Sensor initialized successfully")

except Exception as e:
    print("Initialization failed:", e)
    sys.exit(1)


try:
    # Get current stored templates
    count = f.getTemplateCount()
    print(f"Current stored fingerprints: {count}")

    confirm = input("Are you sure you want to delete ALL fingerprints? (y/n): ")

    if confirm.lower() != 'y':
        print("Operation cancelled")
        sys.exit(0)

    # Delete all templates
    for i in range(0, 20):  # safe upper limit for most sensors
        try:
            if f.deleteTemplate(i):
                print(f"Deleted ID: {i}")
        except Exception:
            # Ignore missing IDs
            pass

    print("All fingerprint memory cleared successfully!")

except Exception as e:
    print("Error while clearing memory:", e)
