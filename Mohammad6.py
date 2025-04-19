async def controller(bus, b, a, vx, _):
    global index, xx, i1, i2, i3

    # On first call, load signals & initialize
    if index is None:
        load_signal_data()
        index = int(np.argmin(np.abs(c - b)))
        xx = heading_array.copy() if (index + 1) % 2 == 1 else (L - heading_array)

        # Bring all to closed, then open if s==1
        for aid in ACTUATOR_IDS:
            await send_act(bus, aid, "close")
        if s1[0] == 1: await send_act(bus, 22, "open")
        if s2[0] == 1: await send_act(bus, 24, "open")
        if s3[0] == 1: await send_act(bus, 26, "open")

    # Compute your target positions and look‑aheads
    w1 = xx[i1] if i1 < len(xx) else None
    w2 = xx[i2] if i2 < len(xx) else None
    w3 = xx[i3] if i3 < len(xx) else None
    lookahead1 = -b - d + (T * 0.9)
    lookahead2 = -b - d

    # Log everything (unchanged)
    log_row(
        a=-a, b=-b,
        s1=s1, s2=s2, s3=s3,
        ds1=ds1, ds2=ds2, ds3=ds3,
        w1=w1, w2=w2, w3=w3,
        i1=i1, i2=i2, i3=i3,
        lookahead1=lookahead1, lookahead2=lookahead2,
        x=latest["x"], y=latest["y"],
        vx=latest["vx"], vy=latest["vy"]
    )

    # --- ACTUATOR 1 ---
    if i1 < len(xx):
        if s1[i1] == 1:
            print(f"[CTRL] ACT22 OPEN @ i1={i1}, w1={w1:.3f}")
            await send_act(bus, 22, "open")
            if lookahead1 > w1:
                i1 += 1
                print(f"       → next i1={i1}")
        else:
            print(f"[CTRL] ACT22 CLOSE @ i1={i1}, w1={w1:.3f}")
            await send_act(bus, 22, "close")
            if lookahead2 > w1:
                i1 += 1
                print(f"       → next i1={i1}")

    # --- ACTUATOR 2 ---
    if i2 < len(xx):
        if s2[i2] == 1:
            print(f"[CTRL] ACT24 OPEN @ i2={i2}, w2={w2:.3f}")
            await send_act(bus, 24, "open")
            if lookahead1 > w2:
                i2 += 1
                print(f"       → next i2={i2}")
        else:
            print(f"[CTRL] ACT24 CLOSE @ i2={i2}, w2={w2:.3f}")
            await send_act(bus, 24, "close")
            if lookahead2 > w2:
                i2 += 1
                print(f"       → next i2={i2}")

    # --- ACTUATOR 3 ---
    if i3 < len(xx):
        if s3[i3] == 1:
            print(f"[CTRL] ACT26 OPEN @ i3={i3}, w3={w3:.3f}")
            await send_act(bus, 26, "open")
            if lookahead1 > w3:
                i3 += 1
                print(f"       → next i3={i3}")
        else:
            print(f"[CTRL] ACT26 CLOSE @ i3={i3}, w3={w3:.3f}")
            await send_act(bus, 26, "close")
            if lookahead2 > w3:
                i3 += 1
                print(f"       → next i3={i3}")

    # Final “close all” if we're past the last waypoint
    if b > -xx[-1]:
        for aid in ACTUATOR_IDS:
            await send_act(bus, aid, "close")

    # Give the bus and actuators a moment
    await asyncio.sleep(0.02)

    # End if all indices done
    if i1 >= len(xx) and i2 >= len(xx) and i3 >= len(xx):
        return
