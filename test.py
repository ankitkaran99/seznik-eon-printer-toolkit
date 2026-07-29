import asyncio
from bleak import BleakClient, BleakScanner

async def probe():
    print("Scanning...")
    device = await BleakScanner.find_device_by_address(
        "D6:E1:B8:2F:71:D8",
        timeout=15,
        bluez={"adapter": "hci1"}
    )
    if not device:
        print("Not found"); return
    
    print(f"Found: {device.address} {device.name}")
    print("Connecting...")
    
    async with BleakClient(
        device,
        bluez={"adapter": "hci1"},
        timeout=20
    ) as client:
        print("Connected!")
        for svc in client.services:
            print(f"\nService: {svc.uuid}")
            for char in svc.characteristics:
                print(f"  Char: {char.uuid} [{','.join(char.properties)}]")

asyncio.run(probe())
