set -e
python -u src/graph.py --manifest M001 --tag pass       --decision approve
python -u src/graph.py --manifest M009 --tag fail       --decision approve
python -u src/graph.py --manifest M005 --tag borderline --decision approve
python -u src/graph.py --manifest M010 --tag declined   --decision reject
echo "ALL TRAJECTORIES CAPTURED"
