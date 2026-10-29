"""Use deterministic Qt rendering unless native Windows checks are requested."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
