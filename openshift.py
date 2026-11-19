"""Render a manual OpenShift deployment for a released HTTP container."""

import argparse
import json
import re


def resources(name, image, port, health_path=None):
    if not re.fullmatch(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", name):
        raise ValueError("Use a lowercase OpenShift resource name up to 63 characters.")
    if not image or any(character.isspace() for character in image):
        raise ValueError("Provide the released container image reference.")
    if not 1 <= port <= 65535 or (health_path and not health_path.startswith("/")):
        raise ValueError("Use a valid port and a health path starting with '/'.")
    labels = {"app.kubernetes.io/name": name}
    container = {
        "name": name, "image": image, "ports": [{"name": "http", "containerPort": port}],
        "env": [{"name": "PORT", "value": str(port)}],
    }
    if health_path:
        container["readinessProbe"] = {"httpGet": {"path": health_path, "port": "http"}}
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "labels": labels},
         "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {"containers": [container]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name},
         "spec": {"selector": labels, "ports": [{"name": "http", "port": port, "targetPort": "http"}]}},
        {"apiVersion": "route.openshift.io/v1", "kind": "Route", "metadata": {"name": name},
         "spec": {"to": {"kind": "Service", "name": name}, "port": {"targetPort": "http"},
                  "tls": {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"}}},
    ]}


if __name__ == "__main__":
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("image")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--health-path")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        result = json.dumps(resources(args.name, args.image, args.port, args.health_path), indent=2) + "\n"
        if args.out:
            with args.out.open("x", encoding="utf-8") as output:
                output.write(result)
        else:
            print(result, end="")
    except (ValueError, OSError) as error:
        parser.exit(1, f"OpenShift manifest generation failed: {error}\n")
