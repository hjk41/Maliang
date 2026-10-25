"""Build-time centerlines. Image/conversion libraries are not runtime dependencies."""
from collections import defaultdict
import numpy as np


def trace_pixels(mask):
    """Trace real skeleton edges, suppressing false staircase diagonals."""
    width = mask.shape[1]
    points = set(np.flatnonzero(mask).tolist())
    adjacency = {}
    for p in points:
        up, right, down, left = p-width, p+1, p+width, p-1
        neighbors = [q for q in (up,right,down,left) if q in points]
        neighbors += [q for q,a,b in ((up+1,up,right),(down+1,down,right),
                       (down-1,down,left),(up-1,up,left))
                      if q in points and a not in points and b not in points]
        adjacency[p] = neighbors
    used, lines = set(), []
    def edge(a,b):
        return (a,b) if a<b else (b,a)
    for start in sorted(points, key=lambda p:(len(adjacency[p])==2,p)):
        for neighbor in adjacency[start]:
            key = edge(start,neighbor)
            if key in used:
                continue
            used.add(key)
            line,previous,current = [start],start,neighbor
            while True:
                line.append(current)
                if len(adjacency[current])!=2 or current==start:
                    break
                nexts = [q for q in adjacency[current]
                         if q!=previous and edge(current,q) not in used]
                if not nexts:
                    break
                used.add(edge(current,nexts[0]))
                previous,current = current,nexts[0]
            lines.append([(p%width,p//width) for p in line])
    isolated = [(p%width,p//width) for p in sorted(points) if not adjacency[p]]
    return lines, isolated


def minimum_trails(segments):
    """Minimum edge-disjoint trails within each connected skeleton.

    Virtual edges are removed before output. White gaps are never bridged.
    """
    adjacency = defaultdict(list)
    edges = {}
    for i,line in enumerate(segments):
        a,b = tuple(line[0]),tuple(line[-1])
        edges[i] = (a,b,line)
        adjacency[a].append((i,b))
        adjacency[b].append((i,a))
    unseen,output,total_used,theoretical = set(adjacency),[],0,0
    while unseen:
        start = min(unseen)
        component,todo = {start},[start]
        while todo:
            for _,neighbor in adjacency[todo.pop()]:
                if neighbor not in component:
                    component.add(neighbor)
                    todo.append(neighbor)
        unseen -= component
        odd = sorted(p for p in component if len(adjacency[p])%2)
        theoretical += max(1,len(odd)//2)
        local = {p:list(adjacency[p]) for p in component}
        virtual = set()
        for j in range(0,len(odd),2):
            eid = -(j//2+1)
            a,b = odd[j:j+2]
            local[a].append((eid,b))
            local[b].append((eid,a))
            virtual.add(eid)
        used,stack,incoming,circuit = set(),[start],[],[]
        while stack:
            p = stack[-1]
            while local[p] and local[p][-1][0] in used:
                local[p].pop()
            if local[p]:
                eid,q = local[p].pop()
                used.add(eid)
                stack.append(q)
                incoming.append((eid,p,q))
            else:
                stack.pop()
                if incoming:
                    circuit.append(incoming.pop())
        circuit.reverse()
        assert all(a[2]==b[1] for a,b in zip(circuit,circuit[1:]))
        if virtual:
            cut = next(i for i,s in enumerate(circuit) if s[0] in virtual)
            circuit = circuit[cut+1:]+circuit[:cut+1]
        path = []
        for eid,a,b in circuit:
            if eid in virtual:
                if path:
                    output.append(path)
                    path = []
                continue
            total_used += 1
            line = edges[eid][2]
            if tuple(line[0])!=a:
                line = line[::-1]
            assert tuple(line[-1])==b
            if path:
                assert tuple(path[-1])==a
                path += line[1:]
            else:
                path = list(line)
        if path:
            output.append(path)
    assert total_used==len(segments) and len(output)==theoretical
    return output


def centerlines(gray, origin, pixels_per_em=512, epsilon=.85):
    import cv2
    from skimage.morphology import skeletonize
    skeleton = skeletonize(np.asarray(gray)<128, method='zhang')
    segments,isolated = trace_pixels(skeleton)
    trails = minimum_trails(segments)
    trails += [[(x-.2,y),(x+.2,y)] for x,y in isolated]
    commands = []
    scale = 1000/pixels_per_em
    for path in trails:
        if len(path)>2:
            path = cv2.approxPolyDP(np.asarray(path,np.float32),epsilon,False).reshape(-1,2).tolist()
        if len(path)==1:
            x,y = path[0]
            path = [(x-.2,y),(x+.2,y)]
        for i,(x,y) in enumerate(path):
            commands.append([int(i>0), round((x+.5+origin[0])*scale,3),
                             round((y+.5+origin[1])*scale,3)])
    return commands
