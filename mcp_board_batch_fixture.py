#!/usr/bin/env python3
"""Create the disposable native board used by the board-batch image smoke test."""
import sys
from pathlib import Path

import pcbnew


def smd_layers():
    layers = pcbnew.LSET()
    for layer in (pcbnew.F_Cu, pcbnew.F_Paste, pcbnew.F_Mask):
        layers.AddLayer(layer)
    return layers


def add_net(board, name):
    net = pcbnew.NETINFO_ITEM(board, name)
    board.Add(net)
    return net


def add_pad_footprint(board, reference, x, y, net):
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference(reference)
    footprint.Reference().SetVisible(False)
    footprint.Value().SetVisible(False)
    footprint.SetPosition(pcbnew.VECTOR2I_MM(x, y))
    pad = pcbnew.PAD(footprint)
    pad.SetNumber("1")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetShape(pcbnew.PAD_SHAPE_RECT)
    pad.SetSize(pcbnew.VECTOR2I_MM(2, 2))
    pad.SetPosition(pcbnew.VECTOR2I_MM(x, y))
    pad.SetLayerSet(smd_layers())
    pad.SetNet(net)
    footprint.Add(pad)
    board.Add(footprint)


def add_track(board, x1, y1, x2, y2, net):
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I_MM(x1, y1))
    track.SetEnd(pcbnew.VECTOR2I_MM(x2, y2))
    track.SetWidth(pcbnew.FromMM(0.5))
    track.SetLayer(pcbnew.F_Cu)
    track.SetNet(net)
    board.Add(track)


def add_edge(board, x1, y1, x2, y2):
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
    edge.SetStart(pcbnew.VECTOR2I_MM(x1, y1))
    edge.SetEnd(pcbnew.VECTOR2I_MM(x2, y2))
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetWidth(pcbnew.FromMM(0.05))
    board.Add(edge)


def main(path):
    board = pcbnew.BOARD()
    net1, net2 = add_net(board, "/N1"), add_net(board, "/N2")
    add_pad_footprint(board, "J1", 20, 20, net1)
    add_pad_footprint(board, "J2", 30, 20, net1)
    add_pad_footprint(board, "J3", 20, 30, net2)
    add_pad_footprint(board, "J4", 30, 30, net2)
    add_track(board, 20, 20, 30, 20, net1)
    add_track(board, 20, 30, 30, 30, net2)
    for points in ((10, 10, 40, 10), (40, 10, 40, 40), (40, 40, 10, 40), (10, 40, 10, 10)):
        add_edge(board, *points)
    pcbnew.SaveBoard(str(path), board)
    assert pcbnew.LoadBoard(str(path)) is not None


main(Path(sys.argv[1]))
