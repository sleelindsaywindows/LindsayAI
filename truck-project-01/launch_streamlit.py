import sys, os

sys.path.insert(0, "/Users/peytonbaker/Library/Python/3.9/lib/python/site-packages")
os.chdir("/Users/peytonbaker/Desktop/LindsayAI/truck-project-01")

port = os.environ.get("PORT", "8501")
sys.argv = ["streamlit", "run", "app.py", "--server.port", port]

from streamlit.web.cli import main
main()
